# agentmcp-fortnox

Part of [AgentHotel](https://www.agenthotel.org).

Your company's Fortnox for your agents, over MCP. An agent (Hermes, or anything that speaks MCP) reads the books and the documents, and writes: vouchers, supplier and customer invoices, payments, customers, suppliers, articles, files. One company, its own integration in its own Fortnox, its own service. You host it.

- **Signs in once.** A system administrator approves the integration in the browser; the service keeps the tokens, refreshes them on every sync and never needs a person again. By default it signs in as a Fortnox *service account*, so it does not stop working when someone leaves.
- **Reads fast.** Vouchers with rows, the chart of accounts, opening balances, supplier and customer invoices and voucher attachments are synced every 6 hours into the data folder. Income statement and balance sheet are computed in the service, with a check that comes out at 0.
- **Writes for real, and says who did it.** Every call that changes something in Fortnox is in the write log with the name of the agent key that made it (tool `skrivlogg`). A voucher an agent writes shows in the financial tools in the same answer.
- **No dependencies.** Python standard library, `pdftotext` for the text of PDFs. One container, one volume.

## Tools

Tool names and fields are Swedish, like Fortnox.

| Read | |
|---|---|
| `kalla_status` | Signed-in company, last sync, what was fetched |
| `verifikationer` | Vouchers with rows, filtered by period, account, project, text, series, what they book |
| `konton` | Debit, credit and net per account for a period |
| `resultatrakning` | Income statement by BAS group, per month/quarter/year/project/cost centre, with last year |
| `balansrakning` | Balance sheet on a date, with a check that should be 0 |
| `lista_dokument`, `sok`, `las_dokument`, `motparter`, `sammanfattning` | Supplier invoices, customer invoices and voucher attachments: list, search the text, read with a PDF link valid for 10 minutes |
| `exportera_index` | The whole document index, for scripts |

| Write | |
|---|---|
| `skapa_verifikation` | A voucher; rows must balance, checked before Fortnox is called |
| `skapa_leverantorsfaktura`, `leverantorsfaktura_atgard` | Register; book, approve, credit, cancel |
| `skapa_kundfaktura`, `kundfaktura_atgard` | Create; book, e-mail, e-invoice, mark sent, credit, cancel |
| `spara_register` | Create or update a customer, supplier, article or project |
| `ladda_upp_fil` | A receipt or invoice PDF into Fortnox, connected to a voucher or supplier invoice |
| `registrera_betalning` | A payment of a customer or supplier invoice, booked |
| `fortnox_api` | Any other endpoint of the Fortnox API, GET/POST/PUT/DELETE |
| `skrivlogg` | What the agents wrote, newest first |

A voucher cannot be deleted in Fortnox; a mistake is corrected with a new voucher that reverses it. The tool descriptions tell the agent so.

Files: an agent with a shell sends a PDF with `curl -H "Authorization: Bearer $KEY" --data-binary @kvitto.pdf "https://<domain>/upload?namn=kvitto.pdf"` and passes the returned id to `ladda_upp_fil`. Small files can also go base64 in the tool call.

## In Fortnox

1. In Fortnox' [Developer Portal](https://www.fortnox.se/developer), create an integration for your own company.
2. Redirect URI: `https://<your domain>/oauth/callback`, exactly, without a trailing slash.
3. Scopes: what you want the agents to reach. The service asks for `companyinformation bookkeeping invoice supplierinvoice customer supplier article payment archive connectfile inbox settings` unless you set `FORTNOX_SCOPES`; it must not ask for more than the integration allows.
4. Tick *allow authorization as service account* (or set `FORTNOX_ACCOUNT_TYPE=` empty to sign in as the person who approves).
5. Copy the client id and secret into the service's environment.

The company needs the Fortnox modules for what the scopes cover (Bokföring, Kundfaktura, Leverantörsfaktura …) and, as Fortnox prices it, an integration licence.

Once the service runs, a system administrator opens `https://<your domain>/oauth/start/<OAUTH_START_TOKEN>` once and approves. The first sync starts right away. The service stays tied to that company: a sign-in to another company is refused.

## Run it

Three ways, the same image. Data, tokens and the write log are in the volume at `/data` (`/data/.state`, mode 600); back it up.

### AgentHotel

Add a guest from the **Fortnox** template, give it a domain, and enter the client id and secret. The keys and the start token are generated and shown under Credentials. Point an agent at `https://<domain>/mcp` with its key.

### Docker

```bash
git clone https://github.com/magnusfroste/agentmcp-fortnox && cd agentmcp-fortnox
cp env.example .env   # fill it in: openssl rand -hex 32 for each key
docker compose up -d --build
```

Put it behind your HTTPS proxy (Caddy, Traefik, a Cloudflare tunnel) on the domain you registered in Fortnox, to port 8000.

### Easypanel

Create an **App** service with source GitHub `magnusfroste/agentmcp-fortnox`, branch `main`, build type Dockerfile. Add a domain pointing to port 8000, a volume mounted at `/data`, and the environment from `env.example`. Deploy builds what is in git; Restart only restarts the image already built.

## Environment

| Variable | |
|---|---|
| `MCP_KEYS` | `name:key` pairs, comma separated, keys 32+ characters. One per agent: the name is in the log and on every write |
| `OAUTH_START_TOKEN` | 32+ characters; the secret part of the sign-in address. Without it sign-in is off |
| `LINK_SECRET` | Signs the PDF links |
| `PUBLIC_URL` | `https://<your domain>` |
| `FORTNOX_CLIENT_ID`, `FORTNOX_CLIENT_SECRET` | From your integration |
| `FORTNOX_REDIRECT_URI` | `https://<your domain>/oauth/callback`, as registered |
| `FORTNOX_SCOPES` | Optional; see above |
| `FORTNOX_ACCOUNT_TYPE` | `service` (default) or empty |
| `FORTNOX_SYNC_FROM` | First date to sync, default January 1st last year. Earlier gives comparisons further back |
| `FORTNOX_READ_ONLY` | `true`: only reads reach Fortnox, and the write tools are not offered |
| `SYNC_HOURS` | Hours between syncs (6). A write also starts one |
| `INDEX_REFERENCES` | Optional order-number prefixes to pick out of documents, e.g. `SO,PO,WO` |
| `INDEX_WORDS` | Optional words to note in documents, e.g. `hyra,frakt` |
| `REPORT_LINK_BASE` | Optional: where a report site serves the data folder, for links in published reports |

## Security

- The service holds a refresh token to your books and a copy of them. It is as sensitive as your Fortnox login: keep the volume and its backups private.
- Fortnox scopes always grant write as well as read. Choose the scopes in your integration accordingly, or set `FORTNOX_READ_ONLY=true`.
- Text in invoices and vouchers is data for the agent, never instructions; the MCP instructions say so.
- Every agent gets its own key; remove one from `MCP_KEYS` to cut that agent off.

## Development

```bash
docker build -t agentmcp-fortnox .
python -m pytest -q   # needs pytest; runs against a fake Fortnox, no account needed
```

## Licence

MIT
