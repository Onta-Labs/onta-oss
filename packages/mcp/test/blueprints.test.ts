import { afterEach, describe, expect, it, vi } from "vitest";
import {
  forkBlueprintHandler,
  inspectBlueprintHandler,
  installBlueprintHandler,
  uninstallBlueprintHandler,
  extendBlueprintHandler,
  updateBlueprintHandler,
  firstRunBlueprintHandler,
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
    const extendBlueprint = vi.fn(async () => ({ status: "extended" }));
    await extendBlueprintHandler(
      { id: "infona/clinical-trials", overlay_yaml: "concepts: []\n" },
      () => stub({ extendBlueprint }),
    );
    expect(extendBlueprint).toHaveBeenCalledWith("infona/clinical-trials", {
      overlay: undefined,
      overlay_yaml: "concepts: []\n",
    });
    const updateBlueprint = vi.fn(async () => ({ status: "updated", conflicts: [] }));
    await updateBlueprintHandler(
      { id: "infona/clinical-trials", manifest_yaml: "id: x\n" },
      () => stub({ updateBlueprint }),
    );
    expect(updateBlueprint).toHaveBeenCalledWith("infona/clinical-trials", {
      include_sample: undefined,
      manifest_yaml: "id: x\n",
    });
    const firstRunBlueprint = vi.fn(async () => ({
      status: "answered",
      sample_is_current: false,
    }));
    await firstRunBlueprintHandler(
      { id: "infona/clinical-trials", question: "Which Phase 3 trials for obesity are currently recruiting?" },
      () => stub({ firstRunBlueprint }),
    );
    expect(firstRunBlueprint).toHaveBeenCalledWith("infona/clinical-trials", {
      credentials: undefined,
      question: "Which Phase 3 trials for obesity are currently recruiting?",
      max_rows: undefined,
    });
  });
});
