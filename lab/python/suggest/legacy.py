"""Case pr-suggest: a comparison to None with == (E711), for the suggestions of P3.

Fails on purpose: do not fix. The live runbook edits one line of this file in a pull request.
"""


def is_missing(value: object) -> bool:
    return value == None


def is_present(value: object) -> bool:
    return value is not None
