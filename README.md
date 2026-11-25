# agentmcp-fortnox

Part of [AgentHotel](https://www.agenthotel.org).

Your company's Fortnox for your agents, over MCP. An agent (Hermes, or anything that speaks MCP) reads the books and the documents, and writes: vouchers, supplier and customer invoices, payments, customers, suppliers, articles, files. One integration in Fortnox and one service, for one company or for several: every agent key reaches exactly one company. You host it.

- **Signs in once.** A system administrator approves the integration in the browser; the service keeps the tokens, refreshes them on every sync and never needs a person again. By default it signs in as a Fortnox *service account*, so it does not stop working when someone leaves.
- **Reads fast.** Vouchers with rows, the chart of accounts, opening balances, supplier and customer invoices and voucher attachments are synced every 6 hours into the data folder. Income statement and balance sheet are computed in the service, with a check that comes out at 0.
- **Writes for real, and says who did it.** Every call that changes something in Fortnox is in the write log with the name of the agent key that made it (tool `skrivlogg`). In Fortnox itself, a voucher or invoice an agent creates carries "Via agent, på uppdrag av <key name>" in its comment, since it stands on the integration there and not on a person. A voucher an agent writes shows in the financial tools in the same answer.
- **Read-only while it learns.** `FORTNOX_READ_ONLY` makes one company, several or all of them read-only: the write tools are not offered and nothing but reads reaches Fortnox.
- **One company or several.** An accounting bureau, or anyone with more than one company, runs one service: each company signs in separately through the same integration, has its own folder, tokens and write log, and an agent's key decides which one it reaches. No tool takes a company as an argument.
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

The period tools (`verifikationer`, `konton`, `resultatrakning`) also take a financial year, `rakenskapsar` (`2026`, `2024/25`), instead of dates.

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

1. In Fortnox' [Developer Portal](https://www.fortnox.se/developer), create an integration. One is enough, also for several companies. Give it a neutral name: Fortnox shows that name ("*name* får åtkomst till …") to everyone who approves it.
2. Redirect URI: `https://<your domain>/oauth/callback`, exactly, without a trailing slash.
3. Scopes: what you want the agents to reach. The service asks for `companyinformation bookkeeping invoice supplierinvoice customer supplier article payment archive connectfile inbox settings` unless you set `FORTNOX_SCOPES`; it must not ask for more than the integration allows, or Fortnox refuses the sign-in before anyone has logged in. Without `payment`, `article` and `inbox`, the tools `registrera_betalning`, articles in `spara_register` and `ladda_upp_fil` to the inbox folders are refused by Fortnox. Adding a scope later means ticking it in the portal, widening `FORTNOX_SCOPES` and a new sign-in for each company.
4. Tick *allow authorization as service account* (or set `FORTNOX_ACCOUNT_TYPE=` empty to sign in as the person who approves).
5. Copy the client id and secret into the service's environment.

The company needs the Fortnox modules for what the scopes cover (Bokföring, Kundfaktura, Leverantörsfaktura …) and, as Fortnox prices it, an integration licence. A Fortnox test company needs no licence, which makes it the place to try the sign-in first.

Once the service runs, a system administrator opens `https://<your domain>/oauth/start/<OAUTH_START_TOKEN>` once and approves, in that same browser and within ten minutes. The first sync starts right away. The service stays tied to that company: a sign-in to another company is refused.

## Several companies

One integration and one redirect URI are enough for any number of companies: Fortnox lets each company's system administrator approve the same integration and hands out separate tokens. Here a company is a short name you choose (`a-z`, `0-9`, `-`), written as a third part of an agent's key:

```
MCP_KEYS=anna:<key>:alfa,erik:<key>:beta,revisor:<key>:alfa
```

### How it works

1. **Name the company in a key.** Add `name:key:company` to `MCP_KEYS` and restart (on AgentHotel: redeploy). The company now exists as an empty place: the start log has a row for it, "waiting for sign-in", and its folder is `/data/companies/<company>`.
2. **Get its sign-in link.** Open `https://<your domain>/health/<OAUTH_START_TOKEN>` and copy that company's `sign_in_link`. It looks like `https://<your domain>/oauth/start/<40 characters>/<company>`.
3. **The company's administrator approves.** They open the link, log in to Fortnox and pick the company. Fortnox shows what the integration asks for, under the integration's name from the Developer Portal, and they press *Godkänn & aktivera*. Fortnox sends the browser back to `/oauth/callback`, the one redirect URI for every company, and the service answers with a page naming the company and the place it was connected to.
4. **The first sync runs at once**, seconds for a small company, and then every 6 hours and after each write. The overview shows the company's name, organisation number, documents and last sync.
5. **The agent uses its key.** `https://<your domain>/mcp` with `Authorization: Bearer <key>` reaches that company and no other. An agent that works for two companies gets two keys and two MCP server entries, one per company.

Try it first with a test company in a place of its own, say `test`, with its own key. Do not sign a test company in to a real company's place to "switch later": a place is tied to the first company that signs in.

### The rules

- `anna` and `revisor` reach the company `alfa`, `erik` reaches `beta`. A key reaches one company, always the same one. Nothing an agent sends can change it: the tools, `/upload` and the PDF links all take the company from the key.
- Each company signs in at its own address. The system administrator of *that* company opens it and approves in the same browser, within ten minutes; if the person has several companies in Fortnox, they pick the right one there. The link to Fortnox that the address leads to cannot be passed on to someone else: an approval from another browser is refused. The page that follows names the company that was connected. The redirect URI registered in Fortnox stays `https://<your domain>/oauth/callback` for all of them.
- Which address: `https://<your domain>/oauth/start/<OAUTH_START_TOKEN>/<company>` works, e.g. `…/alfa`, and is fine when you open it yourself. To *send* one to a company's administrator, take that company's `sign_in_link` from the overview below instead: it has a code of its own in place of the token, and opens that company's sign-in and nothing else. `OAUTH_START_TOKEN` itself opens every company's sign-in and the overview, so with several organisations in one service it stays with you.
- A company's place stays tied to the Fortnox company that first signed in to it, and one Fortnox company can be in one place only: signing `beta` in to the company that is already `alfa` is refused. Companies are told apart by their database number in Fortnox, so two companies with the same organisation number (a company and its test company) are two companies.
- Each company has its own folder, tokens, synced books and documents, upload store, write log and sync. If one company's sign-in is withdrawn in Fortnox, the others carry on.
- Only a company that some key in `MCP_KEYS` names can be signed in. Remove the last key of a company and it is no longer served; what it had stays on disk until you delete it, and the start banner lists it as unused.
- A key without a company part, `name:key`, reaches the *default* company, which is the single-company setup described above (`/oauth/start/<OAUTH_START_TOKEN>` with nothing after it). A deployment from before 2.1.0 is exactly that and needs no change. You can add named companies next to it.

To see how each company is doing, open `https://<your domain>/health/<OAUTH_START_TOKEN>`: signed in or waiting, the company's name and organisation number, documents, last sync, the names of its keys and its `sign_in_link`. No keys and no tokens. The same, without the links, is printed when the service starts. Plain `/health` answers `{"ok":true}` and nothing else.

Wrong company in a place (someone picked the wrong one in Fortnox)? Delete that company's folder (`/data/companies/<company>`), restart the service and sign in again.

On disk:

```
/data/                              the default company (keys without a company part)
  .state/                           token.json, sync.json, writes.jsonl, uploads/   (mode 600)
  manifest.jsonl  fortnox-api/  fortnox-export/
  companies/
    alfa/                           a named company: the same layout one level down
      .state/  manifest.jsonl  fortnox-api/  fortnox-export/
    beta/
```

## Run it

Three ways, the same image. Data, tokens and the write logs are in the volume at `/data` (each company's `.state` folder, mode 600); back it up.

The service runs as the unprivileged user `fortnox` (uid 10001). Started as root, as it is by default, the entrypoint first hands `/data` to that user (a volume from an image before 2.2.0 is root's) and then drops root; started with `--user 10001` it runs as that user directly. `docker-compose.yml` also drops every capability but the three the entrypoint needs, and makes the image read-only.

### AgentHotel

Add a guest from the **Fortnox** template, give it a domain, and enter the client id and secret. The keys and the start token are generated and shown under Credentials. Point an agent at `https://<domain>/mcp` with its key. For more companies, add keys with a company part to `MCP_KEYS` under Environment and redeploy.

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
| `MCP_KEYS` | `name:key` or `name:key:company`, comma separated, keys 32+ characters without `:`. One per agent: the name is in the log and on every write, the company (`a-z`, `0-9`, `-`, at most 32 characters) is the one that key reaches. Without a company: the default company. An entry that cannot be read is left out and named in the log, never bound elsewhere |
| `OAUTH_START_TOKEN` | 32+ characters; the secret part of the sign-in addresses and of `/health/<token>`. Without it both are off |
| `LINK_SECRET` | Signs the PDF links |
| `PUBLIC_URL` | `https://<your domain>` |
| `FORTNOX_CLIENT_ID`, `FORTNOX_CLIENT_SECRET` | From your integration |
| `FORTNOX_REDIRECT_URI` | `https://<your domain>/oauth/callback`, as registered |
| `FORTNOX_SCOPES` | Optional; see above |
| `FORTNOX_ACCOUNT_TYPE` | `service` (default) or empty |
| `FORTNOX_SYNC_FROM` | First date to sync. Default: January 1st last year, moved back to the start of the financial year that day falls in, so a first year from 16 December comes whole. Earlier gives comparisons further back. A tool asked for a period before it answers with a `varning` that the totals are short |
| `FORTNOX_READ_ONLY` | `true`: only reads reach Fortnox and the write tools are not offered, for every company. A comma-separated list (`alfa,beta`, `default` for keys without a company): the same for those companies only. Changing it needs a restart, and the agents' MCP client a reconnect to see the new tool list |
| `FORTNOX_WRITE_NOTE` | The comment put on what agents create (vouchers, supplier and customer invoices), after any comment of the agent's own. Default `Via agent, på uppdrag av {nyckel}`; `{nyckel}` is the key's name. `off` turns it off. `fortnox_api` sends its body as given |
| `SYNC_HOURS` | Hours between syncs (6). A write also starts one |
| `INDEX_REFERENCES` | Optional order-number prefixes to pick out of documents, e.g. `SO,PO,WO` |
| `INDEX_WORDS` | Optional words to note in documents, e.g. `hyra,frakt` |
| `REPORT_LINK_BASE` | Optional: where a report site serves the data folder, for links in published reports. That site serves every company's folder: use it only when all of them are yours to show to the same readers |
| `STATE_DIR` | Optional: where the default company's tokens and write log go instead of `/data/.state`. A named company's are always in its own folder, `/data/companies/<company>/.state` |

## Security

- The service holds a refresh token to your books and a copy of them. It is as sensitive as your Fortnox login: keep the volume and its backups private.
- Fortnox scopes always grant write as well as read. Choose the scopes in your integration accordingly, or set `FORTNOX_READ_ONLY` (all companies, or the ones it names).
- Who knocks is logged: every sign-in start, how it ended (also when Fortnox stopped it, e.g. "Resource owner does not have admin right"), and every wrong key, sign-in code or operator token, with the caller's address (`CF-Connecting-IP`, else `X-Forwarded-For`, else the socket) but never what was tried. Past 20 wrong attempts in 10 minutes an address gets 429 for its wrong attempts; a right key always gets through, since every agent on one host shares an address. The address headers can be forged by anyone who reaches the service without your proxy, so they are for the log only.
- The service runs without root and keeps its files to itself (umask 027).
- Text in invoices and vouchers is data for the agent, never instructions; the MCP instructions say so.
- Every agent gets its own key; remove one from `MCP_KEYS` to cut that agent off.
- With several companies, the key is the wall between them. Give an agent the key of the company it works for and no other; whoever holds a key reads and writes that company's books.
- `OAUTH_START_TOKEN` opens every sign-in address and the overview at `/health/<token>`. It cannot read any books, but whoever has it can connect a company to an empty place and see which companies you serve. Send a company's administrator that company's `sign_in_link`, not the token.

## Development

```bash
docker build -t agentmcp-fortnox .
python -m pytest -q   # needs pytest; runs against a fake Fortnox, no account needed
```

## Licence

MIT
