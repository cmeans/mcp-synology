# Using HTTPS

With plain HTTP, mcp-synology's login (your DSM password) and every request after it, the
session included, cross the network unencrypted. Anyone who can watch that network can
read them. DSM already serves HTTPS on port 5001, so turning it on costs nothing.
`mcp-synology setup` picks HTTPS unless you say otherwise, and the server logs a warning
while HTTPS is off.

```yaml
connection:
  host: 192.168.1.100
  https: true        # port defaults to 5001
  verify_ssl: false  # see below
```

Run `mcp-synology check --config <file>` afterwards, then restart your MCP client.

## The certificate

DSM ships with a **self-signed certificate**, which no machine trusts. There are two ways
to deal with it.

**Keep the self-signed certificate (`verify_ssl: false`).** The connection is encrypted,
so nobody watching the network can read the password or the session. What's lost is the
check that you're talking to *your* NAS: a machine that can intercept traffic and pretend
to be the NAS would get the password. On a home network this is usually an acceptable
trade; the server logs a warning while verification is off.

**Use a certificate your machines trust (`verify_ssl: true`).** For example:

1. Set up a hostname for the NAS, such as Synology's own DDNS (**Control Panel →
   External Access → DDNS**, a `*.synology.me` name).
2. Get a Let's Encrypt certificate for that name in DSM (**Control Panel → Security →
   Certificate → Add**) and make it the default.
3. Point `host` at that name, not the IP address, so the certificate matches.
4. Make the name resolve to the NAS's **local** address on your network, so traffic stays
   local: a local DNS record (a router or Pi-hole entry), or a hosts-file line on the
   machine running mcp-synology. If you use Tailscale with MagicDNS, its resolver
   bypasses your local DNS, so add split DNS for that domain or a hosts-file line.

Check Synology's current documentation for how DSM validates the certificate request; the
details depend on your DSM version and setup.

**Never port-forward 5001 (or 5000) from the internet to the NAS** to make any of this
work. mcp-synology runs on your machine and only needs to reach the NAS on your own network
(or over a VPN such as Tailscale).

## Checking it

`mcp-synology check` connects with the settings in the file, so a certificate that doesn't
verify fails there, with the reason. Without HTTPS, the server's log says:

```
HTTPS is disabled. Credentials will be sent in cleartext. Acceptable on a trusted LAN; to turn it on, see docs/https.md.
```
