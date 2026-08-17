# Security Policy

This project can ingest customer data and talk to a graph database.
Please report vulnerabilities **privately**. Do not open a public issue,
discussion, or pull request.

## How to report

There is no public `security@` mailbox. Use GitHub private vulnerability
reporting:

**https://github.com/infona-ai/infona-oss/security/advisories/new**

If you cannot use GitHub advisories, email the documented org contact
[oss@infona.ai](mailto:oss@infona.ai) with subject `SECURITY:` and a
short title. That is the [infona-ai](https://github.com/infona-ai) org
address, not a 24/7 security inbox — prefer the advisory form.

## What to include

- Affected package or path (`infona_client`, `@infona-ai/cli`, `@infona-ai/mcp`)
- Impact (data exposure, auth bypass, injection, SSRF, …)
- Steps to reproduce, or a proof of concept
- Your preferred credit / contact

## Response time

We are a small team. We will **acknowledge** a report within **3 business
days**. After that we will say whether it is in scope, out of scope, or
needs more information. We do not promise a fix date here. Please give us
a reasonable window to patch before any public disclosure.

## Scope

**In scope:** vulnerabilities in this repository's published code and
default local stack (ingest, HTTP API, CLI, MCP, Neo4j write/query path)
that could leak tenant data, escalate privilege, or compromise a
deployment.

**Out of scope:** denial-of-service against a local process, issues that
require an already-compromised host, and third-party dependency bugs
unless our use of them is the defect (report those upstream).
