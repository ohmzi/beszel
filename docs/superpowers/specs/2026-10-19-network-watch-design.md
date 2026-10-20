# Network Watch: who talks to this server, and what it talks to

Status: approved by the owner, 2026-10-19.
Lives in: `engine/` (the homelab-maint engine) for collection, judging, alerts and blocking,
and `internal/site/` (the OhmzMaintainer dashboard) for the page.

## 1. Why

On 2026-10-17 and 18 someone ran the public Plex2Shell exploit against this server's Plex,
which is exposed on a forwarded port 32400. The server was patched and rejected both
attempts, but they only came to light because the owner asked for a manual check. Nothing
watches network activity today. The owner wants to know who is talking to the server, what
the server and its containers are talking to, and to hear about anyone trying to break in or
use a communication path that isn't meant for them.

Success: the next attempt like the Plex one produces an email within minutes, with the
attacker's address, what they tried, and a block button. On any day, the owner can open one
page and see who has been talking to what, with anything unusual already flagged.

## 2. Goals

1. **Record four kinds of conversation, 30 days of history.**
   - **Internet → server:** connections to the directly exposed port (Plex 32400), plus the
     real client address behind every request that arrives through the Cloudflare tunnel.
   - **Server → internet:** what the host and each container connect out to, by destination
     address, port and, where available, hostname.
   - **Between services:** container-to-container and container-to-host connections.
   - **Home network devices:** LAN devices (192.168.40.0/24) and Tailscale peers reaching
     the server, by device and service.

   Record metadata only: who, when, which service or container, which port, how many
   connections and bytes. Never packet contents. (The owner did not ask for this; it is the
   design's call, for performance and privacy.)

2. **Decide what is odd in two ways (owner's choice: both).**
   - **Learned normal.** A learning period (the design's call: 7 days) records each
     service's usual peers. After that, flag anything new: a container connecting to a
     destination it never used, a service listening or being reached on a new port, a LAN
     device using a service it never used, a container reaching another container or host
     port it has no history with.
   - **Known-bad lists.** Check outside addresses against public threat lists (scanners,
     botnets, miner pools, published indicators such as this week's Plex2Shell ones). Refresh
     them daily.

3. **Specific attack rules on top.** At minimum:
   - Plex logging `unsupported protocol` with `searchOne` in it (this week's attempt pattern).
   - A new file in Plex's Codecs folder with no matching `CodecManager: obtaining decoder`
     line in Plex's log just before it was written. Verified by hand this week: legitimate
     codec downloads are logged about 0.4 seconds earlier.
   - Repeated failed sign-ins on any service that logs them (SSH, Plex, Immich, Nextcloud and
     the other tunnelled apps).
   - **"Meant for them" paths (the design's call; the reviewer pointed out the gap):**
    - **Container → host:** the existing `ufw` pinholes (for example
      `travstats → host Ollama :11434`) are the declared allowed paths. A connection outside
      them, or a blocked attempt at a host port, counts as abuse of a line not meant for that
      sender.
    - **Container → container:** this traffic never passes through the host's `ufw` input
      rules, so pinholes can't judge it. Containers on the same Docker Compose network are
      expected. Anything else is judged against the learned baseline, and is flagged only
      when it is new after the learning period.

4. **Tell the owner in two places (owner's choice).**
   - **Email right away, quiet level (owner's choice).** Only for real attempts: an attack
     rule firing, a known-bad address doing more than a scan, repeated failed sign-ins, or a
     service talking somewhere new. Use the engine's existing notify path and Gmail
     transport, sent only to the owner.
   - **A Homarr widget** with today's picture: counts per direction, top outside talkers,
     open flags. Background scanning shows up here and on the page only, never in email.
   - **No digest (owner's choice).**

5. **One-click block, always approved by the owner (owner's choice: alert + button).**
   - The button appears both in the alert email and on the dashboard page (owner's choice).
   - **The email button never blocks by itself (owner's choice).** It opens the Network page
     behind the Cloudflare Access login with the block pre-filled, and the owner taps Confirm
     there, so a forwarded or leaked email can't apply or lift a block. This differs from
     today's acknowledge email button, which is token-only.
   - The confirmed block is written as a signed request on the same path the acknowledge
     feature uses (`acks.py`, `acks_auth.py`, `ack/inbox`), and the engine applies it. The
     dashboard stays read-only apart from that signed write, as it is today.
   - **Where the block lands depends on how the attacker came in:**
     - Direct port (Plex): a host firewall rule dropping that address.
     - Through the tunnel: a Cloudflare block via the Cloudflare API (owner's choice).
       - Use Cloudflare's per-IP access rules rather than custom firewall rules, because the
         free plan allows only a handful of custom rules (the design's call).
       - The owner creates the API token in the Cloudflare dashboard, scoped to the one
         zone, with permission to edit firewall access rules and nothing else. The builder
         supplies the exact steps.
   - Every block expires (the design's call: 30 days, matching history) unless the owner
     makes it permanent. Every block is listed on the page with an unblock button.

6. **A Network page on the OhmzMaintainer dashboard (owner's choice).**
   - It shows the 30-day history by direction, by service and by outside address, with each
     flag explaining why it fired (which rule, which baseline it broke, which list matched).
   - It also has the block list and the never-block list.

## 3. Anti-goals: what would make this a failure even if it works

- **It must never block family or friends (owner's).** Never-block is learned from sign-ins
  (owner's choice): any address that signed in successfully to Plex, Immich, Nextcloud,
  T'Day or another account-based service in the last 30 days is protected automatically. So
  are the LAN, the Tailscale range and the container networks. A block request against a
  protected address is refused, and the reason is shown instead of applying it.
  - **Owner override (owner's choice).** From the logged-in dashboard, the owner can force a
    block on a protected address by giving a reason. The override is logged.
  - The LAN, Tailscale and container ranges can never be overridden (the design's call), so
    the owner can't lock out the house.
  - When an attack comes from an address that also signed in successfully, the alert says so
    plainly as a possible stolen account, and names the account and service.
- **It must never noticeably slow the server (owner's).** Plex transcoding, the local AI
  models and the GPU jobs come first. The collectors run with a CPU cap like the existing
  live monitor (10%), record summaries instead of every packet, and back off under the
  engine's existing pressure gates.
- **It must not be noisy.** If the owner starts ignoring the emails, it has failed.
  Scanning alone never emails.

## 4. Constraints

- **The server only sees traffic that touches the server.** It can't see what a phone does
  with the rest of the internet; that would need logging on the router. Out of scope.
- **Plex stays on its forwarded port.** The owner will not move it behind Tailscale or the
  tunnel. The design assumes 32400 stays open to the internet.
- **Fit the engine's conventions (owner's choice: build into homelab-maint, no third-party
  engine such as CrowdSec):**
  - Things are added as TOML tables and small Python files (`engine/docs/EXTENDING.md`), and
    anything that changes the host passes the safety checklist there.
  - New state goes under `/var/lib/homelab-maint`.
  - The dashboard reads only the public export, so the page's data is exported like the
    other JSON files.
- **The dashboard is reachable through the tunnel behind Cloudflare Access.** The network
  data names family members' addresses, so it must be at least as protected as the existing
  login-gated acknowledge feature.
- **Notifications go only to the owner (owner's).**
- **No new open ports.**

## 5. Left to the builder

- **How connections are captured.** The host has nftables with kernel logging and
  conntrack, but connection accounting (`nf_conntrack_acct`) is off and the `conntrack` tool
  isn't installed. Docker manages about 30 networks.
- **Where the real client address behind the tunnel comes from.** Candidates are
  cloudflared's own logging and each app's access log reading Cloudflare's client-IP header.
  - **Launch gate (owner's choice): every tunnelled app must record the real client address
    before this goes live.** "Reached via tunnel, address unknown" is not acceptable for any
    app.
  - The plan should start with an inventory of every tunnelled hostname and how each will
    yield the client address. Fixing an app's logging is part of the work, not a follow-up.
- **How outbound hostnames are learned.** For example, from DNS queries made by containers.
- **Which threat lists to use**, and how the learned baseline is stored and aged.
- **Storage format** for the 30 days, within a sensible disk budget.
- **The exact Homarr widget layout and page design**, following the existing `ops-*` widgets
  and OhmzMaintainer pages.
- **How the Cloudflare API token is stored.** It must be kept out of process command lines;
  cloudflared's own token was moved out of `ExecStart` for that reason on 2026-09-14.

## 6. Facts established on 2026-10-19 (after approval)

- **Cloudflare plan and tunnel.**
  - Both zones are on the Free plan.
  - The one tunnel routes **45 public hostnames** to local ports across the two zones.
  - The list isn't written here because this repository is public. Build the inventory at run
    time from the tunnel's configuration using the token below.
  - Plex is reachable both on its forwarded port 32400 and through the tunnel.
- **Security events.**
  - Cloudflare's security events (the `firewallEventsAdaptive` dataset) are readable on the
    Free plan with at least 30 days of history. That matches the 30-day goal, so they are a
    source for Internet → server traffic that Cloudflare already blocked or challenged.
  - In the last 24 hours before this check, they showed managed-rule blocks, Bot Fight
    challenges and Labyrinth injections.
- **Access.**
  - Zero Trust is configured with one-time-PIN login.
  - The only Access application is the one created today for the Ollama hostname (owner email
    only).
  - Every other tunnelled hostname relies on the app's own login. Treat that as the current
    state; changing it is out of scope.
- **Token.**
  - `homelab-maint network-watch` is stored at `/etc/homelab-maint/cloudflare.token`
    (root:root, 0600) and expires 2027-10-19.
  - Verified: it can read zones, IP access rules, security events and the tunnel, and edit
    IP access rules. It can't read DNS, list API tokens or read billing. It can also list
    Access applications, read-only.
  - The engine must read it from that file only. Never pass it on a command line.
