"""Skills that are instructions: a model writes a plan, the platform checks it and runs it.

* ``executor``: the program that runs a plan, in the sandbox, in place of a script; and the language
  of plans (what a placeholder is), which the checks read with the same functions;
* ``checks``: what a plan may do, decided in code from what the skill was granted;
* ``writer``: asking a model for a plan, and once more when the checks refuse it.
"""
