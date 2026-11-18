import { AUTHORITY, expect, test } from "./fixtures";
import {
  callStatus,
  hangUp,
  prepareSimulationCall,
  startCalling,
} from "./simulation";

// The two peers run on one machine with no STUN or mDNS infrastructure.
const chromium = process.env.PW_BROWSER === "chromium";
test.use({
  permissions: chromium ? ["microphone"] : [],
  launchOptions: chromium
    ? {
        args: [
          "--allow-loopback-in-peer-connection",
          "--disable-features=WebRtcHideLocalIpsWithMdns",
          "--use-fake-device-for-media-stream",
        ],
      }
    : {},
});

test("real Dialer audio handoff on successive calls through local WebRTC peers", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await page.addInitScript(() => {
    window.denyTestMicrophone = true;
    Object.defineProperty(navigator.mediaDevices, "getUserMedia", {
      value: async () => {
        if (window.denyTestMicrophone)
          throw new DOMException("Permission denied", "NotAllowedError");
        return new MediaStream();
      },
    });
  });
  await page.route(/\/\.vite\/deps\/@telnyx_webrtc\.js(?:\?|$)/, (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: 'export { TelnyxRTC } from "/tests/telnyx-media-fixture.ts";',
    }),
  );
  let testAudio = false;
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    if (testAudio) json.simulation = false;
    await route.fulfill({ response, json });
  });
  await page.route("**/v1/telephony/agent", async (route) => {
    const response = await route.fetch();
    const { agent_call_control_id } = await response.json();
    await page.evaluate(
      (id) => window.telnyxMedia.offer(id),
      agent_call_control_id,
    );
    await route.fulfill({ response });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Try a simulated workspace" }).click();
  await prepareSimulationCall(page, "Mailbox phone", "+14155550163", [
    { name: "Answered phone", phone: "+14155550164" },
  ]);
  testAudio = true;
  await page.reload();
  const submitted: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/v1/calls/start"))
      submitted.push(request.postDataJSON().idempotency_key);
  });
  await startCalling(page).click();
  await expect(page.getByRole("alert", { name: "Call problem" })).toContainText(
    /cannot use the microphone/i,
  );
  expect(submitted).toEqual([]);
  await page.evaluate(() => (window.denyTestMicrophone = false));

  const startRequest = () =>
    page.waitForResponse((response) =>
      response.url().endsWith("/v1/calls/start"),
    );
  const levels = () => page.evaluate(() => window.telnyxMedia.levels());
  for (const index of [0, 1]) {
    const request = startRequest();
    const button =
      index === 0
        ? startCalling(page)
        : page.getByRole("button", { name: /^Dial now/ });
    await button.click();
    const response = await request;
    expect(response.ok()).toBeTruthy();
    const { id } = await response.json();
    const { authorization } = await response.request().allHeaders();
    const event = async (type: string, payload: object = {}) => {
      const delivered = await page.request.post(
        `${AUTHORITY}/v1/simulation/event`,
        {
          headers: { authorization },
          data: {
            event_id: `${id}-${type}`,
            call_id: id,
            event_type: type,
            payload,
          },
        },
      );
      expect(delivered.ok()).toBeTruthy();
    };
    await expect(hangUp(page)).toBeVisible();
    await expect
      .poll(async () => (await levels()).ringback)
      .toBeGreaterThan(0.02);
    const handoff = page.waitForRequest(
      (request) =>
        request.url().endsWith(`/v1/calls/${id}/playback`) &&
        request.postDataJSON()?.event === "remote_playing",
    );
    await page.evaluate(() => window.telnyxMedia.speak());
    try {
      await expect
        .poll(async () => (await levels()).repHears)
        .toBeGreaterThan(0.02);
    } catch (error) {
      const state = await page.evaluate(() => window.telnyxMedia.status());
      throw new Error(
        `The rep heard no loopback signal: ${JSON.stringify(state)}`,
        { cause: error },
      );
    }
    await expect
      .poll(async () => (await levels()).prospectHears)
      .toBeGreaterThan(0.02);
    await handoff;
    await expect.poll(async () => (await levels()).ringback).toBe(0);
    await expect(callStatus(page)).toContainText(/Dialing|Ringing/);
    await event("call.answered");
    if (index === 0) {
      await event("call.machine.premium.detection.ended:machine", {
        result: "machine",
      });
      await event("call.machine.premium.greeting.ended:beep_detected", {
        result: "beep_detected",
      });
      await expect(callStatus(page)).toContainText("Voicemail detected");
      await expect
        .poll(async () => (await levels()).repHears)
        .toBeGreaterThan(0.02);
      await page.getByRole("button", { name: /^End as voicemail/ }).click();
      await expect(callStatus(page)).toContainText("Voicemail saved");
    } else {
      await event("call.machine.premium.detection.ended:human_residence", {
        result: "human_residence",
      });
      await expect(callStatus(page)).toContainText("Talking · person");
      // A stalled local API must not keep the rep's verified WebRTC leg connected.
      const stopPath = `**/v1/calls/${id}/hangup`;
      let release = () => {};
      const held = new Promise<void>((resolve) => {
        release = resolve;
      });
      await page.route(stopPath, async (route) => {
        await held;
        await route.continue();
      });
      try {
        const stopping = page.waitForRequest(stopPath);
        await hangUp(page).click();
        await stopping;
        await expect
          .poll(() => page.evaluate(() => window.telnyxMedia.stopped.length))
          .toBe(2);
      } finally {
        release();
      }
      await expect(
        page.getByRole("group", { name: "Save outcome" }),
      ).toBeVisible();
    }
    await expect
      .poll(() => page.evaluate(() => window.telnyxMedia.stopped.length))
      .toBe(index + 1);
    expect(submitted).toHaveLength(index + 1);
  }
  expect(new Set(submitted).size).toBe(2);
});
