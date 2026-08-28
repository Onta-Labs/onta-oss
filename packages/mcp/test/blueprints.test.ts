import { afterEach, describe, expect, it, vi } from "vitest";
import {
  forkBlueprintHandler,
  inspectBlueprintHandler,
  installBlueprintHandler,
  uninstallBlueprintHandler,
} from "../src/index.js";

afterEach(() => vi.restoreAllMocks());

function stub(methods: Record<string, ReturnType<typeof vi.fn>>) {
  return methods as unknown as import("@infona-ai/cli").Client;
}

describe("blueprint tools — SDK forwarding", () => {
  it("install / inspect / uninstall call the typed Client methods", async () => {
    const installBlueprint = vi.fn(async () => ({
      status: "installed",
      blueprint_id: "infona/clinical-trials",
    }));
    const inspectBlueprint = vi.fn(async () => ({
      blueprint_id: "infona/clinical-trials",
      sample_is_current: false,
    }));
    const uninstallBlueprint = vi.fn(async () => ({ status: "uninstalled" }));
    await installBlueprintHandler(
      { kg: "clinical-trials", manifest_yaml: "id: x\n" },
      () => stub({ installBlueprint }),
    );
    expect(installBlueprint).toHaveBeenCalledWith({
      kg: "clinical-trials",
      include_sample: true,
      manifest_yaml: "id: x\n",
    });
    await inspectBlueprintHandler(
      { id: "infona/clinical-trials" },
      () => stub({ inspectBlueprint }),
    );
    expect(inspectBlueprint).toHaveBeenCalledWith("infona/clinical-trials");
    await uninstallBlueprintHandler(
      { id: "infona/clinical-trials" },
      () => stub({ uninstallBlueprint }),
    );
    expect(uninstallBlueprint).toHaveBeenCalledWith("infona/clinical-trials");
    const forkBlueprint = vi.fn(async () => ({
      status: "forked",
      blueprint_id: "acme/clinical-trials",
    }));
    await forkBlueprintHandler(
      { id: "infona/clinical-trials", as: "acme/clinical-trials" },
      () => stub({ forkBlueprint }),
    );
    expect(forkBlueprint).toHaveBeenCalledWith("infona/clinical-trials", {
      as: "acme/clinical-trials",
    });
  });
});
