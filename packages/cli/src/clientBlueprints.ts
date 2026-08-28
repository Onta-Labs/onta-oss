/** Blueprint install / inspect / uninstall — canonical `/graphs/{tenant}/blueprints`.

INF-575 / INF-577. Path builders live here so clientHttp stays under the
size budget. RawApi reaches the same builders via ``this.client.pBlueprints``.
*/
import { ClientSkills } from "./clientSkills.js";

export type BlueprintInstallStatus =
  | "installed"
  | "already_installed"
  | "updated";

export interface BlueprintCard {
  blueprint_id: string;
  name: string;
  version: string;
  acquisition_revision: number;
  content_hash: string;
  kg: string;
  installed_at?: string;
  types: string[];
  sample_included: boolean;
  sample_is_current: boolean;
  sample_captured_at?: string | null;
  sample_subject_count?: number;
  skills?: string[];
}

export interface BlueprintInstallResult extends BlueprintCard {
  status: BlueprintInstallStatus;
  tenant_id: string;
  sample_subjects: string[];
}

export interface BlueprintInstallBody {
  kg: string;
  include_sample?: boolean;
  manifest?: Record<string, unknown>;
  manifest_yaml?: string;
}

export interface BlueprintUninstallResult {
  status: "uninstalled";
  blueprint_id: string;
  removed_types: string[];
  removed_sample: string[];
  removed_skills: string[];
}

export class ClientBlueprints extends ClientSkills {
  /** @internal */ pBlueprints(): string {
    return `${this.base()}/blueprints`;
  }
  /** @internal */ pBlueprintsValidate(): string {
    return `${this.pBlueprints()}/validate`;
  }
  /** @internal */ pBlueprintsInstall(): string {
    return `${this.pBlueprints()}/install`;
  }
  /** @internal */ pBlueprint(namespace: string, name: string): string {
    return `${this.pBlueprints()}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`;
  }
  /** @internal */ pBlueprintFork(namespace: string, name: string): string {
    return `${this.pBlueprint(namespace, name)}/fork`;
  }
  /** @internal */ pBlueprintExtend(namespace: string, name: string): string {
    return `${this.pBlueprint(namespace, name)}/extend`;
  }
  /** @internal */ pBlueprintUpdate(namespace: string, name: string): string {
    return `${this.pBlueprint(namespace, name)}/update`;
  }

  async validateBlueprint(body: {
    manifest?: Record<string, unknown>;
    manifest_yaml?: string;
    files?: Record<string, string>;
  }): Promise<{ valid: boolean; errors: string[] }> {
    if (body.files != null || (body.manifest != null && body.manifest_yaml == null)) {
      const got = await this.request<{ errors?: string[] }>(
        "POST",
        this.pBlueprintValidate(),
        { manifest: body.manifest, files: body.files },
      );
      const errors = got.errors ?? [];
      return { valid: errors.length === 0, errors };
    }
    const got = await this.request<{ valid?: boolean; errors?: string[] }>(
      "POST",
      this.pBlueprintsValidate(),
      body,
    );
    const errors = got.errors ?? [];
    return { valid: got.valid ?? errors.length === 0, errors };
  }

  async installBlueprint(body: BlueprintInstallBody): Promise<BlueprintInstallResult> {
    return this.request("POST", this.pBlueprintsInstall(), body);
  }

  async listBlueprints(): Promise<BlueprintCard[]> {
    const got = await this.request<{ blueprints: BlueprintCard[] }>(
      "GET",
      this.pBlueprints(),
    );
    return got.blueprints ?? [];
  }

  async inspectBlueprint(id: string): Promise<BlueprintCard> {
    const { namespace, name } = splitBlueprintId(id);
    return this.request("GET", this.pBlueprint(namespace, name));
  }

  async uninstallBlueprint(id: string): Promise<BlueprintUninstallResult> {
    const { namespace, name } = splitBlueprintId(id);
    return this.request("DELETE", this.pBlueprint(namespace, name));
  }

  async forkBlueprint(
    id: string,
    body: { as?: string } = {},
  ): Promise<unknown> {
    const { namespace, name } = splitBlueprintId(id);
    return this.request("POST", this.pBlueprintFork(namespace, name), body);
  }

  async extendBlueprint(
    id: string,
    body: { overlay?: Record<string, unknown>; overlay_yaml?: string },
  ): Promise<unknown> {
    const { namespace, name } = splitBlueprintId(id);
    return this.request("POST", this.pBlueprintExtend(namespace, name), body);
  }

  async updateBlueprint(
    id: string,
    body: {
      manifest?: Record<string, unknown>;
      manifest_yaml?: string;
      include_sample?: boolean;
    },
  ): Promise<unknown> {
    const { namespace, name } = splitBlueprintId(id);
    return this.request("POST", this.pBlueprintUpdate(namespace, name), body);
  }
}

export function splitBlueprintId(id: string): { namespace: string; name: string } {
  const slash = id.indexOf("/");
  if (slash <= 0 || slash === id.length - 1) {
    throw new Error(`blueprint id must be namespace/name, got ${id}`);
  }
  return { namespace: id.slice(0, slash), name: id.slice(slash + 1) };
}
