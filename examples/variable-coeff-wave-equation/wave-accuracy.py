#!/usr/bin/env python3
"""Runs a multirate accuracy experiment for the 1D wave equation

(This file is here to run the code as a CI job. See the file wave-equation.py
for more experiments.)
"""

import subprocess


if __name__ == "__main__":
    subprocess.call(["./wave-equation.py", "-x", "accuracy"])
