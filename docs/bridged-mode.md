# Bridged mode — the Pi as a remote extender

Requested 2026-08-05. The gateway can flip its network role: instead of
being the HaLow AP with an eth0 uplink, it becomes a **bridge** — it
joins someone else's network as a client (the *uplink*) and serves local
devices (the *downlink*), with NAT pointing at the uplink. A device that
can't speak HaLow reaches the LAN/internet through the Pi.

This is the Pi-scale mirror of the mesh-v4 node bridge
(`docs/halow-bridge-mode.md` in that repo): same idea, more interfaces.

## Roles

| role | uplink | downlink(s) | NAT toward |
|---|---|---|---|
| `gateway` (default) | eth0 (wired LAN) | halow0 (HaLow AP) + wlan0 (mesh-2g) | eth0 |
| `bridge` uplink=halow | halow0 STA joins another gateway's `mesh` SSID | wlan0 (mesh-2g 10.42) + eth0 if eth=downlink (10.43) | halow0 |
| `bridge` uplink=wifi | wlan0 STA joins an upstream WiFi | eth0 (10.43) | wlan0 |

`HALOW_BRIDGE_ETH=mgmt` (default) keeps eth0 as the SSH/management LAN;
`=downlink` turns eth0 into a DHCP-serving downlink (10.43.0.0/24).

## Control

```
halowctl role show                         current role + rollback state
halowctl role check                        render + VALIDATE both configs (nft -c, dnsmasq --test) — no apply
halowctl role wifi-uplink ssid=NAME        set the WiFi-STA uplink profile (PSK on stdin)
halowctl role set-bridge [uplink=halow|wifi] [eth=mgmt|downlink] [hold=N]
halowctl role set-gateway                  restore today's role
halowctl role commit                       keep a bridge role (disarm the rollback)
```

API: `GET /api/role[?check=1]`, `POST /api/config/role` (op=gateway|
bridge|commit; bridge/gateway need confirm=1). Router-tab card.

## The dead-man rollback (why a live switch is safe)

A role switch reconfigures the interface you may be managing through. So
`set-bridge` **arms a transient `halow-role-rollback` timer first**: if
`halowctl role commit` does not run within `hold` seconds (default 300),
the Pi restores the gateway role by itself. Lock yourself out → wait 5
minutes → you're back. `commit` disarms it; the config is validated
(`nft -c`, `dnsmasq --test`) *before* anything live is touched, and an
invalid render is refused with the role left unchanged.

## Status (2026-08-05)

**Config generation, validation, API, UI, and the rollback plumbing are
DONE and bench-verified** [M]: both role configs render and pass
`nft -c` + `dnsmasq --test` on the live Pi; the four uplink/eth topology
variants produce the right NAT direction and downlink subnets; `check`
leaves the live gateway untouched; the rollback timer arms, is detected
by `show`, and `commit` disarms it; role ops require confirm=1.

**The live cutover is not auto-run on this bench** — for the same class
of reason RF features wait on the capacitor:
- `uplink=halow` needs a **second HaLow AP** to join (A5: STA mode has
  never run against a real AP), and joining tears down our own AP.
- `uplink=wifi` puts wlan0 into STA mode; if `eth=downlink` it also
  reshapes eth0 — either can sever remote management, so the first live
  flip wants a **physical console**.
- On `uplink=halow` the wlan0 downlink dnsmasq coexists with
  NetworkManager's mesh-2g `shared` instance; confirm which owns wlan0
  DHCP on the first physical bring-up (likely: hand mesh-2g's DHCP to
  our dnsmasq, or run the downlink on a dedicated SSID).

The dead-man rollback exists precisely so that first physical flip is
recoverable without a reflash.
