# VPN switcher

A small self-hosted web app, run in its own Docker container, for switching the server location (country or city) of a [Gluetun](https://github.com/qdm12/gluetun) VPN container from a phone or browser. It's meant to be reachable only over a private network such as Tailscale.

It changes Gluetun's `SERVER_COUNTRIES` / `SERVER_CITIES` (or regions) for the configured VPN container, restarts the VPN, and shows the current exit IP and location.

Status: early design.

## Design
- One page that lists the locations, each with a switch. The current location is on, and switching on another one turns the old one off and moves the VPN there.
- The app reaches Docker through the Docker socket. The compose stack path and the VPN container name are set in configuration; nothing about the host is hard-coded.
- Containers that share the VPN container's network restart with it, so traffic pauses briefly during a switch. Gluetun's kill switch prevents leaks meanwhile.
- Meant for private-network access only, behind a login. Access to the Docker socket gives full control of the host's containers.

### Locations
- The server list is loaded live from the provider's public server list (AirVPN's status API to start with).
- The list can be filtered by region (for example Europe only).
- One list sorted by country, then city.

### Login
- A password is required.
- Only a bcrypt hash of the password is stored, in `.env` (which is git-ignored). The plain password is never stored.
