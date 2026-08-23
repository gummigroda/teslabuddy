# TeslaBuddy

Connect a [TeslaMate](https://github.com/adriankumpf/teslamate) instance to [Home Assistant](https://www.home-assistant.io/), using MQTT. It allows basic control of your Tesla vehicle via Home Assistant (currently, just starting and stopping charging, and changing the charge limit). GPS/location information is also shown in Home Assistant.

All devices are auto-discovered via Home Assistant using MQTT Auto-Discovery - no manual configuration is required.

This is designed to be run in Docker (like TeslaMate), but can be run standalone if desired using command line arguments.

## Configuraiton

Configuration can be given via the OS environment (or via the command line).

The full list of options can always be viewed by running the script with `-h`. To set an option via the OS environment, convert the command line long option to uppercase, and replace "-" with "\_", for example, the "--database-host" option would be:

```
DATABASE_HOST=postgres.local
```

### Example docker-compose.yml

This example assumes you have built _TeslaBuddy_ as follows:

```
docker build -t teslabuddy .
```

This is a snippet from a `docker-compose.yml` file, this would typically be along side the TeslaMate configuration, and assuming you are running Home Assistant in the same file. Note that the `DATABASE_*` values can match identically the same values used for TeslaMate (you can also create a dedicated user in Postgres with read-only access).

```
  teslabuddy:
    image: teslabuddy
    depends_on:
      - homeassistant
      - teslamate
      - postgres
      - mqtt
    restart: always
    environment:
      - DATABASE_USER=teslamate
      - DATABASE_PASS=securepassword
      - DATABASE_NAME=teslamate
      - DATABASE_HOST=postgres
      - MQTT_HOST=mqtt
      # - MQTT_USER=myuser
      # - MQTT_PASS=mypassword
      # - MQTT_TLS=true
      # - MQTT_TLS_CA_CERT=/certs/ca.crt
      # - MQTT_TLS_INSECURE=true   # skip cert verification (not recommended)
      # - STATUS_TOPIC=teslabuddy  # base topic for status/statistics messages
      # - STATUS_INTERVAL=300      # seconds between status updates, 0 disables
      # - DEBUG=true
    volumes:
      - "/etc/localtime:/etc/localtime:ro"
```

If you are not running TeslaMate as part of the same docker-compose or swarm or have a different name for it, you will also need to include the `TESLAMATE_URL` to match your configuration, eg:

```
      - TESLAMATE_URL=https://teslamate.my.domain/
```

If your TeslaMate configuration also has several vehicles associated with it, you will also need to include the VIN of the desired vehicle, eg:

```
      - VIN=5Y123456789123456
```

### MQTT TLS (mqtts)

To connect to a Mosquitto broker using TLS, set the following environment variables:

```
  teslabuddy:
    ...
    environment:
      - MQTT_HOST=mqtt.my.domain
      - MQTT_TLS=true
      # Port defaults to 8883 when TLS is enabled; override with MQTT_PORT if needed
      # - MQTT_PORT=8883

      # MQTT authentication (recommended with TLS)
      - MQTT_USER=myuser
      - MQTT_PASS=mypassword

      # Provide your CA cert if using a self-signed certificate:
      # - MQTT_TLS_CA_CERT=/certs/ca.crt

      # For mutual TLS (client certificates):
      # - MQTT_TLS_CERT=/certs/client.crt
      # - MQTT_TLS_KEY=/certs/client.key

      # Skip certificate verification (not recommended for production):
      # - MQTT_TLS_INSECURE=true
    volumes:
      - "/etc/localtime:/etc/localtime:ro"
      # Mount certs if using MQTT_TLS_CA_CERT / MQTT_TLS_CERT / MQTT_TLS_KEY:
      # - "/path/to/certs:/certs:ro"
```

### Status and health monitoring

teslabuddy publishes its own state to MQTT, under `STATUS_TOPIC` (default `teslabuddy`), per VIN:

- `teslabuddy/<VIN>/availability` — `online` / `offline` (retained, `offline` is set as the MQTT last will, so it is published by the broker if teslabuddy dies). All Home Assistant entities use this as their availability topic.
- `teslabuddy/<VIN>/status` — retained JSON published at startup and every `STATUS_INTERVAL` seconds (default 300, set to `0` to disable):

```json
{
  "state": "online",
  "vin": "5Y123456789123456",
  "car_name": "Tessie",
  "teslamate_car_id": 1,
  "started": 1740000000,
  "uptime_seconds": 3720,
  "uptime": "1h 2m",
  "teslamate_messages": 1043,
  "last_teslamate_message_seconds": 4,
  "messages_published": 512,
  "tesla_api_commands": 3,
  "timestamp": 1740003720
}
```

The same summary is written to the container log, and it is exposed in Home Assistant as a diagnostic "TeslaBuddy Uptime" sensor with the JSON fields as attributes.

The Docker image also includes a `HEALTHCHECK`: teslabuddy refreshes `/tmp/teslabuddy.healthy` (override with `HEALTH_FILE`) only while it is connected to MQTT, so a broken connection marks the container unhealthy. Combined with `restart: always`, this can be used with a supervisor such as [autoheal](https://github.com/willfarrell/docker-autoheal) to restart the container automatically.

### Docker Secrets

Sensitive values (passwords) can be provided via [Docker Secrets](https://docs.docker.com/engine/swarm/secrets/) using the standard `_FILE` suffix convention. If `FOO_FILE` is set to a file path and `FOO` is **not** set, the file contents are used as the value for `FOO`. This works for any configuration option:

```yaml
  teslabuddy:
    ...
    environment:
      - DATABASE_PASS_FILE=/run/secrets/teslamate_db_password
      - MQTT_PASS_FILE=/run/secrets/mqtt_password
    secrets:
      - teslamate_db_password
      - mqtt_password

secrets:
  teslamate_db_password:
    external: true
  mqtt_password:
    external: true
```



Home Assistant does **not** use its configured home location to set this for device trackers via MQTT (I'm not sure about other devices).

# ToDo

Currently this only supports charging actions. If you are interested in supporting more actions, please raise an issue.

# Implementation Notes

This implementation uses the vehicle VIN as as identifier for everything to make sure it remains unique. This means that should a vehicle be move to/from another account, things should remain the same in Home Assistant, even if TeslaMate needs updating.

I use this to manage when my car will charge, to make the most of home solar generation and cheap grid prices (adjusting how much the vehicle will charge based on the current value).
