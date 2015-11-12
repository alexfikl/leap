"""Adams-Bashforth ODE solvers."""

from __future__ import division

__copyright__ = """
Copyright (C) 2007 Andreas Kloeckner
Copyright (C) 2014, 2015 Matt Wala
Copyright (C) 2015 Cory Mikida
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import numpy  # noqa
from leap import Method


__doc__ = """
.. autoclass:: AdamsBashforthMethod
"""


class AdamsBashforthMethod(Method):
    """
    User-supplied context:
        <state> + component_id: The value that is integrated
        <func> + component_id: The right hand side
    """

    def __init__(self, component_id, order, state_filter_name=None):
        super(AdamsBashforthMethod, self).__init__()
        self.order = order

        from pymbolic import var

        self.component_id = component_id

        # Declare variables
        self.step = var('<p>step')
        self.function = var('<func>' + component_id)
        self.rhs = var('<p>f_n')
        self.history = \
            [var('<p>f_n_minus_' + str(i)) for i in range(self.order - 1, 0, -1)]
        self.time_history = \
            [var('<p>t_n_minus_' + str(i)) for i in range(self.order - 1, 0, -1)]
        self.state = var('<state>' + component_id)
        self.t = var('<t>')
        self.dt = var('<dt>')

        if state_filter_name is not None:
            self.state_filter = var("<func>" + state_filter_name)
        else:
            self.state_filter = None

    def generate(self):
        from dagrt.language import DAGCode, CodeBuilder
        from pymbolic import var

        # Initialization
        with CodeBuilder(label="initialization") as cb_init:
            cb_init(self.step, 1)

        steps = self.order

        # Primary
        with CodeBuilder(label="primary") as cb_primary:

            time_history = self.time_history + [self.t]

            cb_primary("n", len(time_history))
            cb_primary.fence()

            cb_primary("start_time", self.t)
            cb_primary.fence()
            cb_primary("end_time", self.t + self.dt)
            cb_primary.fence()

            cb_primary("time_history", "`<builtin>array`(n)")
            cb_primary.fence()

            for i in range(len(time_history)):
                cb_primary("time_history[{0}]".format(i), time_history[i])
                cb_primary.fence()

            cb_primary("point_eval_vec", "`<builtin>array`(n)")
            cb_primary("vdm_transpose", "`<builtin>array`(n*n)")
            cb_primary.fence()

            cb_primary("point_eval_vec[g]",
                    "1 / (g + 1) * (end_time ** (g + 1)- start_time ** (g + 1)) ",
                    loops=[("g", 0, "n")])
            cb_primary("vdm_transpose[g*n + h]", "time_history[g]**h",
                    loops=[("g", 0, "n"), ("h", 0, "n")])

            cb_primary.fence()
            cb_primary("new_coeffs",
                    "`<builtin>linear_solve`(vdm_transpose, point_eval_vec, n, 1)")

            # Define a Python-side vector for the calculated coefficients

            new_coeffs_pyvar = var("new")
            cb_primary.fence()

            new_coeffs_py = [new_coeffs_pyvar[i] for i in range(len(time_history))]

            # Use a loop to assign each element of this vector to an element
            # from our newly calculated coeff vector (Fortran-side)

            cb_primary("new", "`<builtin>array`(n)")
            cb_primary.fence()

            for i in range(len(time_history)):
                cb_primary(new_coeffs_py[i], "new_coeffs[{0}]".format(i))
                cb_primary.fence()

            cb_primary(self.rhs, self.eval_rhs(self.t, self.state))
            cb_primary.fence()
            history = self.history + [self.rhs]
            ab_sum = sum(new_coeffs_pyvar[i] * history[i] for i in range(steps))

            state_est = self.state + ab_sum
            if self.state_filter is not None:
                state_est = self.state_filter(state_est)
            cb_primary(self.state, state_est)

            # Rotate history and time history.
            for i in range(len(self.history)):
                cb_primary.fence()
                cb_primary(self.history[i], history[i + 1])
                cb_primary(self.time_history[i], time_history[i + 1])
                cb_primary.fence()
            cb_primary(self.t, self.t + self.dt)
            cb_primary.yield_state(expression=self.state,
                                   component_id=self.component_id,
                                   time_id='', time=self.t)

        if steps == 1:
            # The first order method requires no bootstrapping.
            return DAGCode.create_with_init_and_step(
                instructions=cb_init.instructions | cb_primary.instructions,
                initialization_dep_on=cb_init.state_dependencies,
                step_dep_on=cb_primary.state_dependencies)

        # Bootstrap
        with CodeBuilder(label="bootstrap") as cb_bootstrap:
            self.rk_bootstrap(cb_bootstrap)
            cb_bootstrap(self.t, self.t + self.dt)
            cb_bootstrap.yield_state(expression=self.state,
                                     component_id=self.component_id,
                                     time_id='', time=self.t)
            cb_bootstrap(self.step, self.step + 1)
            with cb_bootstrap.if_(self.step, "==", steps):
                cb_bootstrap.state_transition("primary")

        states = {}
        states["initialization"] = cb_init.as_execution_state("bootstrap")
        states["bootstrap"] = cb_bootstrap.as_execution_state("bootstrap")
        states["primary"] = cb_primary.as_execution_state("primary")

        return DAGCode(
            instructions=cb_init.instructions | cb_bootstrap.instructions |
            cb_primary.instructions,
            states=states,
            initial_state="initialization")

    def eval_rhs(self, t, y):
        """Return a node that evaluates the RHS at the given time and
        component value."""
        from pymbolic.primitives import CallWithKwargs
        return CallWithKwargs(function=self.function,
                              parameters=(),
                              kw_parameters={"t": t, self.component_id: y})

    def rk_bootstrap(self, cb):
        """Initialize the timestepper with an RK method."""

        cb(self.rhs, self.eval_rhs(self.t, self.state))

        # Save the current RHS to the AB history

        for i in range(len(self.history)):
            with cb.if_(self.step, "==", i + 1):
                cb(self.history[i], self.rhs)

        for i in range(len(self.time_history)):
            with cb.if_(self.step, "==", i + 1):
                cb(self.time_history[i], self.t)

        from leap.rk import ORDER_TO_RK_METHOD
        rk_method = ORDER_TO_RK_METHOD[self.order]
        rk_tableau = tuple(zip(rk_method.c, rk_method.a_explicit))
        rk_coeffs = rk_method.output_coeffs

        # Stage loop (taken from EmbeddedButcherTableauMethod)
        from pymbolic import var
        rhss = [var("rk_rhs_" + str(i)) for i in range(len(rk_tableau))]
        for stage_num, (c, coeffs) in enumerate(rk_tableau):
            if len(coeffs) == 0:
                assert c == 0
                cb(rhss[stage_num], self.rhs)
            else:
                stage = self.state + sum(self.dt * coeff * rhss[j]
                                         for (j, coeff)
                                         in enumerate(coeffs))

                if self.state_filter is not None:
                    stage = self.state_filter(stage)

                cb(rhss[stage_num], self.eval_rhs(self.t + c * self.dt, stage))

        # Merge the values of the RHSs.
        rk_comb = sum(coeff * rhss[j] for j, coeff in enumerate(rk_coeffs))
        cb.fence()

        state_est = self.state + self.dt * rk_comb
        if self.state_filter is not None:
            state_est = self.state_filter(state_est)

        # Assign the value of the new state.
        cb(self.state, state_est)
