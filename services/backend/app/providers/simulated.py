from uuid import uuid4


class UncertainCarrierResult(TimeoutError):
    """Submission may have succeeded. Reconcile; never automatically redial."""


class SimulatedCarrier:
    simulated = True

    def __init__(self):
        self.actions = []
        self.dials = []
        self.playbacks = []
        self.hangups = []
        self.purchases = []
        self.calls = {}
        self.numbers = {}
        self.timeout_after_accept = False

    async def dial(self, payload):
        if payload["command_id"] in self.calls:
            return self.calls[payload["command_id"]]
        self.dials.append(dict(payload))
        result = {"call_control_id": "sim-" + str(uuid4()), "state": "ringing"}
        self.calls[payload["command_id"]] = result
        if self.timeout_after_accept:
            raise UncertainCarrierResult("Simulated response lost after provider acceptance")
        return result

    async def reconcile(self, command_id, provider_id=None):
        return self.calls.get(command_id)

    async def hangup(self, provider_id, command_id):
        self.hangups.append({"provider_id": provider_id, "command_id": command_id})
        for call in self.calls.values():
            if call["call_control_id"] == provider_id:
                call["state"] = "hangup"
        return {"status": "ok"}

    async def playback(self, provider_id, payload):
        self.playbacks.append({"provider_id": provider_id, **payload})
        return {"status": "accepted"}

    async def action(self, provider_id, action, payload):
        self.actions.append({"provider_id": provider_id, "action": action, "payload": payload})
        return {"status": "accepted", "action": action}

    async def token(self, credential_id):
        return "simulation-scoped-credential"

    async def list_numbers(self):
        return [dict(n) for n in self.numbers.values()]

    async def configure_number(self, number_id, connection_id):
        self.numbers[number_id] = {**self.numbers[number_id], "connection_id": connection_id}
        return dict(self.numbers[number_id])

    async def get_number(self, number_id):
        return dict(self.numbers[number_id])

    async def search_numbers(self, region):
        return [
            {
                "phone_number": "+1" + region + "5550188",
                "cost_information": {"currency": "USD", "upfront_cost": "0.00", "monthly_cost": "1.00"},
            }
        ]

    async def purchase(self, numbers, command_id):
        self.purchases.append({"numbers": numbers, "command_id": command_id})
        return {"id": command_id, "phone_numbers": numbers}

    async def lookup(self, phone):
        return {"phone_number": phone, "carrier": {"name": "SIMULATED"}}
