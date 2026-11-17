"""Hooks and their helpers read from the call tree (Robinhood 102937418 #6): never a secondary operator."""

from mevlab.trace.calls import hook_machinery

MANAGER, LOCKER, HOOK, HELPER, TOKEN = ("0x" + c * 40 for c in "abcde")


def frame(frm, to, sel, *calls):
    return {"type": "CALL", "from": frm, "to": to, "input": sel + "00" * 4, "calls": list(calls)}


def test_hooks_and_their_helpers_from_the_call_tree():
    helper_swap = frame(HELPER, MANAGER, "0xf3cd914c", frame(MANAGER, HOOK, "0xb47b2fb1"))
    trace = frame(
        "0x" + "1" * 40,
        LOCKER,
        "0x12345678",
        frame(
            LOCKER,
            MANAGER,
            "0x48c89491",
            frame(
                MANAGER,
                LOCKER,
                "0x91dd7346",  # unlockCallback to the locker
                frame(
                    LOCKER,
                    MANAGER,
                    "0xf3cd914c",  # the user's swap
                    frame(
                        MANAGER,
                        HOOK,
                        "0xb47b2fb1",  # afterSwap
                        frame(HOOK, HELPER, "0x167d1604", helper_swap),
                    ),
                ),
                frame(MANAGER, TOKEN, "0xa9059cbb"),
            ),
        ),
    )  # a token payment
    assert hook_machinery(trace) == {HOOK, HELPER}


def test_no_manager_no_hooks():
    assert (
        hook_machinery(frame("0x" + "1" * 40, LOCKER, "0x12345678", frame(LOCKER, TOKEN, "0xa9059cbb")))
        == set()
    )
