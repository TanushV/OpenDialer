"""Choose the carrier connection: the test carrier under simulation, otherwise Telnyx from saved setup."""

from fastapi import HTTPException

from app.providers.telnyx import TelnyxCarrier


def carrier_factory(settings, configs, simulated):
    def carrier(tenant_id, http=None):
        if settings.simulation:
            return simulated
        conf = configs.read(tenant_id)
        if not conf.get("telnyx_api_key") or not conf.get("telnyx_connection_id"):
            raise HTTPException(503, "TELNYX_NOT_CONFIGURED")
        return TelnyxCarrier(
            conf["telnyx_api_key"],
            conf["telnyx_connection_id"],
            webhook_url=conf.get("webhook_url", settings.webhook_public_url),
            http=http,
        )

    return carrier
