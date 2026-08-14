/** InfonaError + env lookup used by the SDK client. */
export class InfonaError extends Error {
  status?: number;
  body?: string;

  constructor(message: string, opts?: { status?: number; body?: string }) {
    super(message);
    this.name = "InfonaError";
    this.status = opts?.status;
    this.body = opts?.body;
  }
}


export function envVar(name: string, fallback?: string): string | undefined {
  return process.env[`INFONA_${name}`] || fallback;
}

export const EXT_FORMAT: Record<string, string> = {
  ".csv": "csv",
  ".json": "json",
  ".jsonl": "json",
  ".txt": "text",
};
