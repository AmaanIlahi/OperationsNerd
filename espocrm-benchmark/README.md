# EspoCRM benchmark

Local EspoCRM instance used to verify that config-pack-driven custom fields
(e.g. `referral_source` on Contact) actually land in EspoCRM as expected.

## Setup

1. Copy `.env.example` to `.env` and set real values:
   ```
   cp .env.example .env
   ```
2. Start the stack:
   ```
   docker compose up -d
   ```
3. Wait for the `espocrm-db` healthcheck to pass, then open
   `http://localhost:8080` and log in with `admin` / the password you set
   in `.env`.

The instance is bound to `127.0.0.1:8080` only -- it is not reachable from
other machines on the network.

## Verifying a custom field

1. In EspoCRM, go to Administration > Entity Manager > Contact > Fields
   and confirm the field (e.g. `referral_source`) exists with the expected
   type and options.
2. Open any Contact record, edit it, and confirm the field appears in the
   detail layout (`custom/Espo/Custom/Resources/layouts/Contact/detail.json`)
   and saves correctly.
3. Confirm the field's label renders via
   `custom/Espo/Custom/Resources/i18n/en_US/Contact.json` instead of the
   raw field name.

## Tearing down

```
docker compose down -v
```

The `-v` flag also removes the `espocrm-db` and `espocrm-data` volumes, so
only use it when you want a clean slate.
