"""The connector templates themselves (ONTA-555). Data only — no logic.

Each entry prefills the generic REST / SQL extract (see ``catalog.py`` for the
boundary rules). Paths and auth types come from each vendor's PUBLIC API docs
and are **not** verified against any particular account or plan: they stay
editable in the connect drawer, and a wrong path surfaces as the usual
"upstream 404 — check base_url and resource paths" error.

Adding a template is a data-only change: append a :class:`ConnectorTemplate`
here. It must (a) use https, (b) keep resource paths relative, (c) name a
BYOK credential, and (d) never carry a token — ``tests/test_extract_catalog.py``
enforces all four.
"""

from __future__ import annotations

from infona_client.ingestion.catalog import (
    ConnectorAuth,
    ConnectorPlaceholder,
    ConnectorResource,
    ConnectorTemplate,
)


def _r(path: str, label: str, type_name: str, id_field: str = "id", default: bool = True):
    return ConnectorResource(
        path=path, label=label, suggested_type=type_name, id_field=id_field, default=default
    )


CONNECTORS: tuple[ConnectorTemplate, ...] = (
    ConnectorTemplate(
        id="hubspot",
        title="HubSpot",
        category="crm",
        kind="rest_api",
        blurb="Contacts, companies and deals from the HubSpot CRM API.",
        docs_url="https://developers.hubspot.com/docs/api/crm/understanding-the-crm",
        base_url="https://api.hubapi.com",
        auth=ConnectorAuth(
            type="bearer",
            label="Private app token",
            help="HubSpot → Settings → Integrations → Private Apps. Needs the crm.objects.*.read scopes.",
        ),
        resources=[
            _r("crm/v3/objects/contacts", "Contacts", "Contact"),
            _r("crm/v3/objects/companies", "Companies", "Company"),
            _r("crm/v3/objects/deals", "Deals", "Deal"),
        ],
    ),
    ConnectorTemplate(
        id="salesforce",
        title="Salesforce",
        category="crm",
        kind="rest_api",
        blurb="Accounts and contacts via SOQL queries against the REST API.",
        docs_url="https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/",
        base_url="https://{instance}.my.salesforce.com",
        placeholders=[
            ConnectorPlaceholder(
                key="instance",
                label="My Domain",
                example="acme",
                help="From your Salesforce URL: https://<this>.my.salesforce.com",
            )
        ],
        auth=ConnectorAuth(
            type="bearer",
            label="OAuth access token",
            help="A connected-app access token. Session ids from the SOAP login flow also work.",
        ),
        resources=[
            _r(
                "services/data/v60.0/query?q=SELECT+Id,Name,Email+FROM+Contact",
                "Contacts (SOQL)",
                "Contact",
                id_field="Id",
            ),
            _r(
                "services/data/v60.0/query?q=SELECT+Id,Name,Industry+FROM+Account",
                "Accounts (SOQL)",
                "Account",
                id_field="Id",
            ),
        ],
        note="Salesforce reads are SOQL queries — edit the SELECT list to pull the fields you care about.",
    ),
    ConnectorTemplate(
        id="pipedrive",
        title="Pipedrive",
        category="crm",
        kind="rest_api",
        blurb="People, organizations and deals from the Pipedrive v1 API.",
        docs_url="https://developers.pipedrive.com/docs/api/v1",
        base_url="https://api.pipedrive.com/v1",
        auth=ConnectorAuth(
            type="api_key",
            label="API token",
            api_key_header="x-api-token",
            help="Pipedrive → Personal preferences → API.",
        ),
        resources=[
            _r("persons", "People", "Person"),
            _r("organizations", "Organizations", "Organization"),
            _r("deals", "Deals", "Deal"),
        ],
    ),
    ConnectorTemplate(
        id="stripe",
        title="Stripe",
        category="payments",
        kind="rest_api",
        blurb="Customers, charges, subscriptions and products from Stripe.",
        docs_url="https://docs.stripe.com/api",
        base_url="https://api.stripe.com",
        auth=ConnectorAuth(
            type="bearer",
            label="Secret key",
            help="Use a restricted key with read-only permissions (rk_…) where you can.",
        ),
        resources=[
            _r("v1/customers", "Customers", "Customer"),
            _r("v1/subscriptions", "Subscriptions", "Subscription"),
            _r("v1/products", "Products", "Product"),
            _r("v1/charges", "Charges", "Charge", default=False),
        ],
    ),
    ConnectorTemplate(
        id="shopify",
        title="Shopify",
        category="commerce",
        kind="rest_api",
        blurb="Customers, orders and products from a Shopify store.",
        docs_url="https://shopify.dev/docs/api/admin-rest",
        base_url="https://{store}.myshopify.com/admin/api/2024-10",
        placeholders=[
            ConnectorPlaceholder(
                key="store",
                label="Store handle",
                example="acme-supply",
                help="From your admin URL: https://<this>.myshopify.com",
            )
        ],
        auth=ConnectorAuth(
            type="api_key",
            label="Admin API access token",
            api_key_header="X-Shopify-Access-Token",
            help="Create a custom app in the Shopify admin and grant it read scopes.",
        ),
        resources=[
            _r("customers.json", "Customers", "Customer"),
            _r("orders.json", "Orders", "Order"),
            _r("products.json", "Products", "Product"),
        ],
    ),
    ConnectorTemplate(
        id="zendesk",
        title="Zendesk",
        category="support",
        kind="rest_api",
        blurb="Tickets, users and organizations from Zendesk Support.",
        docs_url="https://developer.zendesk.com/api-reference/ticketing/introduction/",
        base_url="https://{subdomain}.zendesk.com",
        placeholders=[
            ConnectorPlaceholder(
                key="subdomain",
                label="Zendesk subdomain",
                example="acme",
                help="From https://<this>.zendesk.com",
            )
        ],
        auth=ConnectorAuth(
            type="basic",
            label="API token",
            username_label="Email + /token",
            help="Username is you@example.com/token; the password is the API token.",
        ),
        resources=[
            _r("api/v2/tickets.json", "Tickets", "Ticket"),
            _r("api/v2/users.json", "Users", "User"),
            _r("api/v2/organizations.json", "Organizations", "Organization"),
        ],
    ),
    ConnectorTemplate(
        id="intercom",
        title="Intercom",
        category="support",
        kind="rest_api",
        blurb="Contacts, companies and conversations from Intercom.",
        docs_url="https://developers.intercom.com/docs/references/rest-api/",
        base_url="https://api.intercom.io",
        auth=ConnectorAuth(
            type="bearer",
            label="Access token",
            help="Intercom → Settings → Developers → your app → Access token.",
        ),
        resources=[
            _r("contacts", "Contacts", "Contact"),
            _r("companies", "Companies", "Company"),
            _r("conversations", "Conversations", "Conversation", default=False),
        ],
    ),
    ConnectorTemplate(
        id="github",
        title="GitHub",
        category="dev",
        kind="rest_api",
        blurb="Repositories and issues visible to a personal access token.",
        docs_url="https://docs.github.com/en/rest",
        base_url="https://api.github.com",
        headers={"Accept": "application/vnd.github+json"},
        auth=ConnectorAuth(
            type="bearer",
            label="Personal access token",
            help="A fine-grained token with read access to the repos you want.",
        ),
        resources=[
            _r("user/repos", "Your repositories", "Repository"),
            _r("issues", "Issues assigned to you", "Issue", default=False),
        ],
    ),
    ConnectorTemplate(
        id="sentry",
        title="Sentry",
        category="dev",
        kind="rest_api",
        blurb="Projects and organizations from Sentry.",
        docs_url="https://docs.sentry.io/api/",
        base_url="https://sentry.io/api/0",
        auth=ConnectorAuth(
            type="bearer",
            label="Auth token",
            help="Sentry → Settings → Auth Tokens, with project:read scope.",
        ),
        resources=[
            _r("projects/", "Projects", "Project", id_field="slug"),
            _r("organizations/", "Organizations", "Organization", id_field="slug"),
        ],
    ),
    ConnectorTemplate(
        id="jira",
        title="Jira",
        category="project",
        kind="rest_api",
        blurb="Projects and issues from Jira Cloud.",
        docs_url="https://developer.atlassian.com/cloud/jira/platform/rest/v3/",
        base_url="https://{site}.atlassian.net",
        placeholders=[
            ConnectorPlaceholder(
                key="site",
                label="Atlassian site",
                example="acme",
                help="From https://<this>.atlassian.net",
            )
        ],
        auth=ConnectorAuth(
            type="basic",
            label="API token",
            username_label="Atlassian account email",
            help="Create the token at id.atlassian.com → Security → API tokens.",
        ),
        resources=[
            _r("rest/api/3/project/search", "Projects", "Project"),
            _r("rest/api/3/search?jql=order+by+created", "Issues", "Issue", id_field="key"),
        ],
    ),
    ConnectorTemplate(
        id="asana",
        title="Asana",
        category="project",
        kind="rest_api",
        blurb="Workspaces, projects and users from Asana.",
        docs_url="https://developers.asana.com/reference/rest-api-reference",
        base_url="https://app.asana.com/api/1.0",
        auth=ConnectorAuth(
            type="bearer",
            label="Personal access token",
            help="Asana → My settings → Apps → Manage developer apps.",
        ),
        resources=[
            _r("projects", "Projects", "Project", id_field="gid"),
            _r("workspaces", "Workspaces", "Workspace", id_field="gid"),
            _r("users", "Users", "User", id_field="gid", default=False),
        ],
    ),
    ConnectorTemplate(
        id="mailchimp",
        title="Mailchimp",
        category="marketing",
        kind="rest_api",
        blurb="Audiences and campaigns from Mailchimp Marketing.",
        docs_url="https://mailchimp.com/developer/marketing/api/",
        base_url="https://{dc}.api.mailchimp.com/3.0",
        placeholders=[
            ConnectorPlaceholder(
                key="dc",
                label="Data centre",
                example="us21",
                help="The suffix of your API key, e.g. …-us21.",
            )
        ],
        auth=ConnectorAuth(
            type="basic",
            label="API key",
            username_label="Any username",
            username_default="infona",
            help="Mailchimp accepts any username with the API key as the password.",
        ),
        resources=[
            _r("lists", "Audiences", "MailingList"),
            _r("campaigns", "Campaigns", "Campaign"),
        ],
    ),
    ConnectorTemplate(
        id="airtable",
        title="Airtable",
        category="productivity",
        kind="rest_api",
        blurb="Records from one Airtable base — add a resource per table.",
        docs_url="https://airtable.com/developers/web/api/list-records",
        base_url="https://api.airtable.com/v0/{base_id}",
        placeholders=[
            ConnectorPlaceholder(
                key="base_id",
                label="Base ID",
                example="appXXXXXXXXXXXXXX",
                help="Airtable → Help → API documentation shows the base id.",
            )
        ],
        auth=ConnectorAuth(
            type="bearer",
            label="Personal access token",
            help="Create at airtable.com/create/tokens with data.records:read.",
        ),
        resources=[],
        note="Add one resource per table, using the table name as the path (e.g. Contacts).",
    ),
    ConnectorTemplate(
        id="postgres",
        title="PostgreSQL",
        category="database",
        kind="sql",
        blurb="Read tables straight out of a Postgres database.",
        docs_url="https://dlthub.com/docs/dlt-ecosystem/verified-sources/sql_database",
        auth=ConnectorAuth(
            type="none",
            label="Connection string",
            help="postgresql://user:password@host:5432/dbname — stored encrypted, never echoed.",
        ),
        resources=[],
        note="List the tables to read. A read-only role is strongly recommended.",
    ),
    ConnectorTemplate(
        id="mysql",
        title="MySQL",
        category="database",
        kind="sql",
        blurb="Read tables straight out of a MySQL database.",
        docs_url="https://dlthub.com/docs/dlt-ecosystem/verified-sources/sql_database",
        auth=ConnectorAuth(
            type="none",
            label="Connection string",
            help="mysql+pymysql://user:password@host:3306/dbname — stored encrypted, never echoed.",
        ),
        resources=[],
        note="List the tables to read. A read-only role is strongly recommended.",
    ),
    ConnectorTemplate(
        id="custom_rest",
        title="Custom REST API",
        category="custom",
        kind="rest_api",
        blurb="Any JSON REST API. Bring the base URL, a token and the paths.",
        base_url="",
        auth=ConnectorAuth(type="bearer", label="API token", help="Left empty for public APIs."),
        resources=[],
        custom=True,
    ),
    ConnectorTemplate(
        id="custom_sql",
        title="Custom SQL database",
        category="custom",
        kind="sql",
        blurb="Any SQLAlchemy-reachable database. Bring a DSN and table names.",
        auth=ConnectorAuth(
            type="none",
            label="Connection string",
            help="Stored encrypted, never echoed back.",
        ),
        resources=[],
        custom=True,
    ),
)
