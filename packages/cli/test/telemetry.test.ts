import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  CONSENT_BLURB,
  envTelemetryOverride,
  isTelemetryEnabled,
  maybePromptTelemetryConsent,
  readTelemetryState,
  writeTelemetryState,
  type TelemetryPromptIo,
} from "../src/telemetry.js";

let home: string;
const originalHome = process.env.HOME;
const originalUserProfile = process.env.USERPROFILE;
const originalTel = process.env.INFONA_TELEMETRY;
const originalState = process.env.INFONA_TELEMETRY_STATE;

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "infona-tel-"));
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  delete process.env.INFONA_TELEMETRY;
  delete process.env.INFONA_TELEMETRY_STATE;
});

afterEach(() => {
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  if (originalUserProfile === undefined) delete process.env.USERPROFILE;
  else process.env.USERPROFILE = originalUserProfile;
  if (originalTel === undefined) delete process.env.INFONA_TELEMETRY;
  else process.env.INFONA_TELEMETRY = originalTel;
  if (originalState === undefined) delete process.env.INFONA_TELEMETRY_STATE;
  else process.env.INFONA_TELEMETRY_STATE = originalState;
  try {
    rmSync(home, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
});

function scriptedIo(answers: string[]): TelemetryPromptIo & { written: string } {
  const io: TelemetryPromptIo & { written: string } = {
    written: "",
    isTty: true,
    write: (s) => {
      io.written += s;
    },
    question: async () => answers.shift() ?? "",
  };
  return io;
}

describe("CLI telemetry consent", () => {
  it("is disabled by default", () => {
    expect(envTelemetryOverride()).toBeNull();
    expect(isTelemetryEnabled()).toBe(false);
    expect(readTelemetryState()).toBeNull();
  });

  it("INFONA_TELEMETRY=0 wins over a yes consent file", () => {
    writeTelemetryState({
      opt_in: true,
      asked: true,
      install_id: "i1",
    });
    process.env.INFONA_TELEMETRY = "0";
    expect(isTelemetryEnabled()).toBe(false);
    expect(envTelemetryOverride()).toBe(false);
  });

  it("INFONA_TELEMETRY=1 enables without a prompt", () => {
    process.env.INFONA_TELEMETRY = "1";
    expect(isTelemetryEnabled()).toBe(true);
  });

  it("first-run TTY yes writes opt_in true", async () => {
    const io = scriptedIo(["y"]);
    const state = await maybePromptTelemetryConsent({ io });
    expect(state?.opt_in).toBe(true);
    expect(state?.asked).toBe(true);
    expect(state?.install_id).toBeTruthy();
    expect(io.written).toContain("anonymous");
    expect(CONSENT_BLURB).toContain("never sent");
    const onDisk = JSON.parse(
      readFileSync(join(home, ".infona", "telemetry.json"), "utf-8"),
    );
    expect(onDisk.opt_in).toBe(true);
    expect(onDisk.asked).toBe(true);
  });

  it("first-run TTY no writes opt_in false", async () => {
    const io = scriptedIo(["n"]);
    const state = await maybePromptTelemetryConsent({ io });
    expect(state?.opt_in).toBe(false);
    expect(isTelemetryEnabled()).toBe(false);
  });

  it("does not prompt when not a TTY", async () => {
    const io = scriptedIo(["y"]);
    io.isTty = false;
    const state = await maybePromptTelemetryConsent({ io });
    expect(state).toBeNull();
    expect(isTelemetryEnabled()).toBe(false);
  });

  it("does not prompt again after asked", async () => {
    writeTelemetryState({ opt_in: false, asked: true, install_id: "x" });
    const io = scriptedIo(["y"]);
    const state = await maybePromptTelemetryConsent({ io });
    expect(state?.opt_in).toBe(false);
    expect(io.written).toBe("");
  });

  it("skips the prompt when env already decides", async () => {
    process.env.INFONA_TELEMETRY = "0";
    const io = scriptedIo(["y"]);
    await maybePromptTelemetryConsent({ io });
    expect(io.written).toBe("");
    expect(isTelemetryEnabled()).toBe(false);
  });
});
