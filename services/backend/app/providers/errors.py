"""Carrier outcomes every carrier connection reports the same way."""


class UncertainCarrierResult(TimeoutError):
    """Submission may have succeeded. Reconcile; never automatically redial."""


class CarrierNotSubmitted(RuntimeError):
    """The request provably never reached the carrier, so failing it cannot hide a live call."""
