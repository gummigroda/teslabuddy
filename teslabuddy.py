#!/usr/bin/env python3
"""
Connect a TeslaMate instance to Home Assistant, using MQTT

TeslaMate: https://github.com/adriankumpf/teslamate
Home Assistant: https://www.home-assistant.io/

Configuration can be given via the OS environment (or via the command line), by
converting the command line long option to uppercase, and replacing "-" with "_",
for example, the "--database-host" option would be:

DATABASE_HOST=postgres.local

Like TeslaMate, this is designed to be run in Docker, but can be run standalone
if required using command line options.
"""
import os
import sys
import argparse
import queue
import time
import json
import logging
import threading
import postgres

import paho.mqtt.client
import requests

# Number of retries to the Tesla API
COMMAND_RETRIES = 3
# Number of seconds between each retry attempt, scaled by 1.5 between each attempt
COMMAND_RETRY_DELAY = 10

# Number of seconds to cache the token from the TeslaMate DB
TOKEN_CACHE_TIME = 30

# Pub cache time in seconds
PUBLISH_CACHE_TIME = 3600

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s: %(levelname)s:%(name)s: %(message)s"
)
log = logging.getLogger(__name__)

# Included in every HA discovery message to identify this integration
ORIGIN = {
    "name": "teslabuddy",
    "support_url": "https://github.com/gummigroda/teslabuddy",
}

GPS_TOPICS = {"elevation", "location", "longitude", "geofence", "latitude", "speed", "heading"}
MAP_THROUGH_TOPICS = {
    "battery_level",
    "charge_current_request",
    "charge_current_request_max",
    "charge_energy_added",
    "charge_limit_soc",
    "charger_actual_current",
    "charger_phases",
    "charger_power",
    "charger_voltage",
    "charging_state",
    "climate_keeper_mode",
    "est_battery_range_km",
    "ideal_battery_range_km",
    "inside_temp",
    "odometer",
    "outside_temp",
    "rated_battery_range_km",
    "scheduled_charging_start_time",
    "state",
    "time_to_full_charge",
    "tpms_pressure_fl",
    "tpms_pressure_fr",
    "tpms_pressure_rl",
    "tpms_pressure_rr",
    "usable_battery_level",
    "version",
}
# Topics that TeslaMate publishes as "true"/"false" strings mapped to ON/OFF for HA
BOOLEAN_TOPICS = {
    "charge_port_door_open",
    "doors_open",
    "is_climate_on",
    "is_preconditioning",
    "locked",
    "plugged_in",
    "sentry_mode",
    "tpms_soft_warning_fl",
    "tpms_soft_warning_fr",
    "tpms_soft_warning_rl",
    "tpms_soft_warning_rr",
    "update_available",
    "windows_open",
}


class TeslaBuddy:
    def __init__(self) -> None:
        self.config = self._initconfig()
        self.gpsq = queue.Queue()
        self.teslapiq = queue.Queue()
        self.teslamateq = queue.Queue()
        # Store target command state in a dict so several commands will replace each
        # other rather than an internal queue where all updates would eventually hit
        # the Tesla API, potentially resulting in ratelimits getting hit sooner.
        self._pubstate = {}
        self._pubcacheexpiry = time.time() + PUBLISH_CACHE_TIME

        self._tokencache = {}

        self.tmid: int = -1
        self.eid: int = -1
        self.carname: str | None = None
        self.carmodeltxt: str | None = None
        self.error_sleep_time = COMMAND_RETRY_DELAY
        self.teslamatesettings = None

        if self.config.wake_topics:
            self.wake_topics = set(self.config.wake_topics.split())
        else:
            self.wake_topics = set()

        self.basetopic = ""

        self.teslamatesetup()

    def teslamatesetup(self):
        """Figure out the TeslaMate car ID and name from the VIN (if set)

        If VIN is not set, defaults to 1
        If not found, an error is raised.
        """
        if self.config.vin is None:
            whereclause = "settings_id = 1"
            whereargs = []
        else:
            whereclause = "vin = %s"
            whereargs = [self.config.vin]

        cardata = self.getdbconn().one(
            f"SELECT settings_id, vin, eid, name, trim_badging, model FROM cars WHERE {whereclause}",
            whereargs,
        )
        if cardata is None:
            raise ValueError(
                f"The VIN {self.config.vin} or car id 1 was not found in the TeslaMate database"
            )
        self.tmid = cardata[0]
        self.vin = cardata[1]
        self.eid = cardata[2]
        self.carname = cardata[3]
        if self.carname is None:
            raise ValueError("Car name is not set in TeslaMate!")
        self.carmodeltxt = f"Model {cardata[5]} {cardata[4]}"

        self.teslamatesettings = self.getdbconn().one("SELECT * FROM settings LIMIT 1;")

        self.basetopic = self.config.base_topic
        while self.basetopic.endswith("/"):
            self.basetopic = self.basetopic[:-1]
        self.basetopic += "/" + self.vin

    def getdbconn(self) -> postgres.Postgres:
        "Return a connection to the TeslaMate DB"
        dburl = f"postgres://{self.config.database_user}:{self.config.database_pass}@{self.config.database_host}:{self.config.database_port}/{self.config.database_name}"
        conn = postgres.Postgres(dburl)
        return conn

    def start(self):
        self.client = paho.mqtt.client.Client(
            callback_api_version=paho.mqtt.client.CallbackAPIVersion.VERSION1
        )
        self.client.on_connect = self.onmqttconnect
        self.client.on_message = self.onmqttmessage

        if self.config.mqtt_user:
            self.client.username_pw_set(self.config.mqtt_user, self.config.mqtt_pass)

        port = self.config.mqtt_port
        use_tls = bool(self.config.mqtt_tls and self.config.mqtt_tls.lower() == "true")

        if use_tls:
            ca_certs = self.config.mqtt_tls_ca_cert or None
            certfile = self.config.mqtt_tls_cert or None
            keyfile = self.config.mqtt_tls_key or None
            self.client.tls_set(
                ca_certs=ca_certs,
                certfile=certfile,
                keyfile=keyfile,
            )
            if (
                self.config.mqtt_tls_insecure
                and self.config.mqtt_tls_insecure.lower() == "true"
            ):
                self.client.tls_insecure_set(True)

        if port is None:
            port = 8883 if use_tls else 1883

        self.client.connect(self.config.mqtt_host, port)
        self.client.loop_start()

        # Thread to manage bundling GPS information into a single message
        threading.Thread(target=self.gpsbundlethread, daemon=True).start()
        # Thread to manage waking TeslaMate when incoming commands happen
        threading.Thread(target=self.waketeslamatethread, daemon=True).start()

        self.homeassistantsetup()
        # Run the tesla thread, resume on error
        while 1:
            try:
                self.teslacomandthread()
            except Exception as e:
                log.exception("Error in tesla thread: %s", e)
                # traceback.print_exc()
                log.info("Sleeping %0.1f seconds from error", self.error_sleep_time)
                time.sleep(self.error_sleep_time)
                self.error_sleep_time *= 1.5

    def onmqttconnect(self, client, userdata, flags, rc):
        self.client.subscribe(f"{self.basetopic}/+/set")
        self.client.subscribe(f"teslamate/cars/{self.tmid}/+")
        for topic in self.wake_topics:
            self.client.subscribe(topic)

    def onmqttmessage(self, client, userdata, msg):
        payload = msg.payload.decode()
        topic: str = msg.topic
        parts = topic.split("/")

        log.debug("Incomming MQTT Message: %s : %s", topic, payload)
        if topic in self.wake_topics:
            self.waketeslamate()
        elif topic.startswith("teslamate/cars/"):
            self.teslamatemsg(parts[3], payload)
        elif topic.startswith(self.basetopic):
            if parts[-1] == "set":
                self.teslapiq.put((parts[-2], payload))

    def mqtt_publish(self, topic, payload, retain=False):
        log.debug("Publishing MQTT Message: %s : %s", topic, payload)
        self.client.publish(topic, payload, retain=retain)

    def teslamatemsg(self, topic, value):
        "Process as message from TeslaMate"
        if topic in GPS_TOPICS:
            self.gpsq.put((topic, value))

        elif topic in MAP_THROUGH_TOPICS:
            self.pubifchanged(topic, value)

        elif topic in BOOLEAN_TOPICS:
            normalized = str(value).strip().lower()
            self.pubifchanged(topic, "ON" if normalized == "true" else "OFF")

        elif topic == "shift_state":
            if not value:
                value = "P"
            self.pubifchanged(topic, value)

        # else:
        # print("TBC:", topic, value)

        if topic == "state":
            # Also update the charging/not charging switch
            if value == "charging":
                txt = "ON"
            else:
                txt = "OFF"
            self.pubifchanged("charging", txt)

    def gpsbundlethread(self):
        "Waits for a full 'batch' of GPS location values before sending to HASS"
        # Time to wait for any more messages to come in to populate the current_state
        # TeslaMate sends all updates (on different topics) at the same moment
        QUEUE_TIMEOUT = 0.1
        current_state = {
            "latitude": None,
            "heading": 0,
            "longitude": None,
            "geofence": "",
            "speed": 0,
            "elevation": 0,
            "state": "not_home",  # Used by HASS, matches "Home" TeslaMate geofence
            "gps_accuracy": 1,  # HASS requires this, always set to 1
        }

        timeout = None
        while 1:
            try:
                topic, value = self.gpsq.get(block=True, timeout=timeout)
            except queue.Empty:
                # New data has come in, with no updates, broadcast to HASS
                timeout = None
                if (
                    current_state["latitude"] is None
                    or current_state["longitude"] is None
                ):
                    # Don't try to send anyting if the lat/long is not set
                    continue
                self.pubifchanged("gps", json.dumps(current_state))
                continue

            if topic == "location":
                # TeslaMate combines lat/lon in a single JSON topic (deprecated separate topics removed)
                try:
                    loc = json.loads(value)
                    current_state["latitude"] = forcefloat(loc.get("latitude"))
                    current_state["longitude"] = forcefloat(loc.get("longitude"))
                except (json.JSONDecodeError, TypeError, KeyError):
                    log.warning("Failed to parse location JSON: %r", value)

            elif topic == "geofence":
                current_state["geofence"] = value
                if value.lower() == "home":
                    current_state["state"] = "home"
                else:
                    current_state["state"] = "not_home"

            elif topic in ("elevation", "longitude", "latitude", "speed", "heading"):
                current_state[topic] = forcefloat(value)

            timeout = QUEUE_TIMEOUT

    def waketeslamate(self):
        "Wake TeslaMate right away to get latest information"
        self.teslamateq.put("wake")

    def waketeslamatethread(self):
        """Attempts to wake the TeslaMate thread when required

        This ignores errors, and will not retry.
        """
        while 1:
            try:
                self.teslamateq.get(block=True, timeout=None)
                while self.teslamateq.qsize() > 0:
                    # Empty the queue to prevent rapid multiple requests
                    self.teslamateq.get(block=False)
                if not self.config.teslamate_url.startswith("http"):
                    log.debug(
                        "Clearly invalid TeslaMate URL, ignoring: %r",
                        self.config.teslamate_url,
                    )
                    continue
                baseurl = self.config.teslamate_url
                while baseurl.endswith("/"):
                    baseurl = baseurl[:-1]
                url = f"{baseurl}/api/car/{self.tmid}/logging/resume"
                log.debug("Waking TeslaMate at URL: %s", url)
                requests.put(url)
                # Slow down repeated requests
                time.sleep(1)
            except Exception as e:
                log.debug("Error making call to TeslaMate: %s", e)

    def _initconfig(self):
        parser = argparse.ArgumentParser(
            description="Connect TeslaMate to Home Assistant via MQTT.\n"
            "Unknown arguments are ignored (including typos)",
        )
        parser.add_argument(
            "--database-host",
            help="host name of the Postgres server",
            required=True,
        )
        parser.add_argument(
            "--database-user",
            help="username to access the Postgres server - this can be a read only user",
            required=True,
        )
        parser.add_argument(
            "--database-pass",
            help="password for the --database-user",
            required=True,
        )
        parser.add_argument(
            "--database-name",
            help="name of the database in postgres to connect to",
            required=True,
        )
        parser.add_argument(
            "--database-port",
            help="port of the postgres server to connect to",
            default=5432,
            type=int,
        )

        parser.add_argument(
            "--mqtt-host",
            help="MQTT broker host name",
            required=True,
        )
        parser.add_argument(
            "--mqtt-port",
            help="MQTT broker port (defaults to 8883 when --mqtt-tls is true, otherwise 1883)",
            default=None,
            type=int,
        )
        parser.add_argument(
            "--mqtt-user",
            help="MQTT broker username",
        )
        parser.add_argument(
            "--mqtt-pass",
            help="MQTT broker password",
        )
        parser.add_argument(
            "--mqtt-tls",
            help='if set to "true", enable TLS for the MQTT connection (mqtts)',
        )
        parser.add_argument(
            "--mqtt-tls-ca-cert",
            help="path to CA certificate file for MQTT TLS verification",
        )
        parser.add_argument(
            "--mqtt-tls-cert",
            help="path to client certificate file for mutual TLS authentication",
        )
        parser.add_argument(
            "--mqtt-tls-key",
            help="path to client private key file for mutual TLS authentication",
        )
        parser.add_argument(
            "--mqtt-tls-insecure",
            help='if set to "true", disable TLS certificate verification (not recommended)',
        )

        parser.add_argument(
            "--teslamate-url",
            help="base URL for TeslaMate, default is http://teslamate:4000/",
            default="http://teslamate:4000/",
        )

        parser.add_argument(
            "--vin",
            help="the VIN of the desired vehicle, "
            "only required if more than one vehicle on the account",
        )

        parser.add_argument(
            "--base-topic",
            help="base MQTT topic for pub/sub messages, no trailing /",
            default="tesla/car",
        )

        parser.add_argument(
            "--wake-topics",
            help="a space separated list of MQTT topics to subscribe to and use to "
            "wake up TeslaMate. If any of the topics are called with any value, "
            "the TeslaMate API will be called to cancel sleep. "
            "Used for example when the garage door is opened",
        )

        parser.add_argument(
            "--debug",
            help='if set to "true", will include debug level logging',
        )

        # Get the OS environnement arguments, with Docker secret support:
        # If FOO_FILE=/run/secrets/foo is set (and FOO is not set directly),
        # the contents of that file are used as the value for FOO.
        secret_overrides = {}
        for key, path in os.environ.items():
            if key.endswith("_FILE") and path:
                base_key = key[:-5]
                if not os.environ.get(base_key):
                    try:
                        with open(path) as f:
                            secret_overrides[base_key] = f.read().strip()
                        log.debug("Loaded secret for %s from %s", base_key, path)
                    except OSError as e:
                        log.warning(
                            "Could not read secret file for %s (%s): %s",
                            base_key, path, e,
                        )

        cmdlineargs = sys.argv.copy()[1:]
        merged_env = {**os.environ, **secret_overrides}
        for arg, val in merged_env.items():
            if arg.endswith("_FILE"):
                continue  # skip the _FILE pointer itself
            arg = arg.lower().replace("_", "-")
            if val:
                cmdlineargs.append(f"--{arg}={val}")
            else:
                cmdlineargs.append(f"--{arg}")

        args = parser.parse_known_args(cmdlineargs)[0]
        if args.debug:
            if args.debug.lower() == "true":
                args.debug = True
            logging.getLogger().setLevel(logging.DEBUG)
        if args.debug is not True:
            args.debug = False
        # log.debug("Processed command line arguments: %s", cmdlineargs)
        log.debug("Final arguments: %s", args)
        return args

    def gettoken(self):
        """Get the current Tesla token from TeslaMate

        This does some basic minor caching of the token
        """
        if time.time() > self._tokencache.get("expiry", 1):
            self._tokencache["token"] = self.getdbconn().one(
                "SELECT access FROM tokens LIMIT 1;"
            )
            self._tokencache["expiry"] = time.time() + TOKEN_CACHE_TIME
        return self._tokencache["token"]

    def teslacomandthread(self):
        """Send off any comand requests to the Tesla API, including retrys

        Keeps a target state so if several updates for the same value come through
        only the current value is set if having issues/delays
        """
        targetstate = {}
        timeout = None
        errortimeout = COMMAND_RETRY_DELAY
        errortries = 0
        while 1:
            setting = None
            try:
                setting = self.teslapiq.get(block=True, timeout=timeout)
            except queue.Empty:
                pass

            if setting:
                # Got a setting, store and read out anything else left in queue
                while 1:
                    targetstate[setting[0]] = setting[1]
                    try:
                        setting = self.teslapiq.get(block=False)
                    except queue.Empty:
                        break

            if not targetstate:
                timeout = None
                continue

            try:
                # XXX Check permissions
                key, value = list(targetstate.items())[0]
                if key == "charge_limit_soc":
                    val = forceint(value)
                    if val >= 50 and val <= 100:
                        # Valid, make the request
                        self.teslaapireq(
                            "set_charge_limit", {"percent": val}, ["already_set"]
                        )

                elif key == "charging":
                    if value == "ON":
                        self.teslaapireq(
                            "charge_start", okreasons=["charging", "complete"]
                        )
                    elif value == "OFF":
                        self.teslaapireq("charge_stop", okreasons=["not_charging"])

                # If we got here, no errors were raised, remove it from the state
                del targetstate[key]
                errortimeout = COMMAND_RETRY_DELAY
                errortries = 0
                self.waketeslamate()
            except Exception as e:
                log.info("Error making Tesla API call: %s", e)
                log.debug("Sleeping %s seconds", errortimeout)
                time.sleep(errortimeout)
                timeout = 1
                errortimeout *= 1.5
                errortries += 1

            if errortries >= COMMAND_RETRIES:
                raise Exception("Command error retries hit, existing loop")

    def teslaapireq(self, command, payload={}, okreasons=[]):
        """Make a request to the Tesla API, no errors if all OK"""
        r = requests.post(
            f"https://owner-api.teslamotors.com/api/1/vehicles/{self.eid}/command/{command}",
            json=payload,
            headers={"Authorization": f"Bearer {self.gettoken()}"},
        )
        response = r.json()["response"]
        if "error" in response:
            # {"response": None, "error": '{"error": "timeout"}', "error_description": ""}
            errtxt = (
                f"{response['error']} {response.get('error_description', '')}".strip()
            )
            raise requests.RequestException(f"Tesla API Error: {errtxt}")
        if response["result"] is True:
            return
        elif response["reason"] in okreasons:
            return

        raise requests.RequestException(f"Tesla API Error: {response['reason']}")

    def pubifchanged(self, item: str, value: str):
        """Publish to MQTT item (self.basetopic will be applied), with value.

        An item is only published if it has changed from when previously published.

        Cache is cleared about every hour.
        """
        if time.time() > self._pubcacheexpiry:
            log.debug("Clearing publish cache")
            self._pubstate.clear()
            self._pubcacheexpiry = time.time() + PUBLISH_CACHE_TIME

        if self._pubstate.get(item) != value:
            self.mqtt_publish(f"{self.basetopic}/{item}", value)
            self._pubstate[item] = value

    def homeassistantsetup(self):
        "Publish config for Home Assistant MQTT auto-discovery"
        temp_unit = "°" + self.teslamatesettings.unit_of_temperature
        length_unit = self.teslamatesettings.unit_of_length

        # Device blocks — HA requires at least identifiers + name.
        # device_full is used for the first entity to register full device metadata;
        # device_ref is used for all subsequent entities (HA deduplicates by identifiers).
        device_full = {
            "identifiers": [f"{self.vin}_device"],
            "name": f"{self.carname} Vehicle",
            "manufacturer": "Tesla",
            "model": self.carmodeltxt,
            "serial_number": self.vin,
        }
        device_ref = {
            "identifiers": [f"{self.vin}_device"],
            "name": f"{self.carname} Vehicle",
        }

        # Entities. Names omit the car name — HA automatically prepends the device name.
        # Format keys: topic, type, name, uom, device_class, icon, state_class, entity_category
        ENTITIES = [
            # --- State & Info ---
            {"topic": "state", "type": "sensor", "name": "State",
             "icon": "mdi:gauge"},
            {"topic": "shift_state", "type": "sensor", "name": "Shift State"},
            {"topic": "charging_state", "type": "sensor", "name": "Charging State",
             "icon": "mdi:ev-station"},
            {"topic": "version", "type": "sensor", "name": "Software Version",
             "entity_category": "diagnostic"},
            {"topic": "climate_keeper_mode", "type": "sensor", "name": "Climate Mode",
             "icon": "mdi:fan"},
            {"topic": "scheduled_charging_start_time", "type": "sensor",
             "name": "Scheduled Charge Time", "icon": "mdi:clock-outline"},
            # --- Temperature ---
            {"topic": "outside_temp", "type": "sensor", "name": "Outside Temperature",
             "uom": temp_unit, "device_class": "temperature",
             "state_class": "measurement"},
            {"topic": "inside_temp", "type": "sensor", "name": "Inside Temperature",
             "uom": temp_unit, "device_class": "temperature",
             "state_class": "measurement"},
            # --- Battery & Range ---
            {"topic": "usable_battery_level", "type": "sensor",
             "name": "Usable Battery Level", "uom": "%", "device_class": "battery",
             "state_class": "measurement"},
            {"topic": "odometer", "type": "sensor", "name": "Odometer",
             "uom": length_unit, "icon": "mdi:counter",
             "state_class": "total_increasing"},
            {"topic": "est_battery_range_km", "type": "sensor",
             "name": "Estimated Range", "uom": "km",
             "icon": "mdi:map-marker-distance"},
            {"topic": "rated_battery_range_km", "type": "sensor",
             "name": "Rated Range", "uom": "km",
             "icon": "mdi:map-marker-distance"},
            {"topic": "ideal_battery_range_km", "type": "sensor",
             "name": "Ideal Range", "uom": "km",
             "icon": "mdi:map-marker-distance"},
            # --- Charging ---
            {"topic": "time_to_full_charge", "type": "sensor",
             "name": "Time to Full Charge", "uom": "h", "device_class": "duration",
             "icon": "mdi:clock-fast"},
            {"topic": "charge_energy_added", "type": "sensor", "name": "Energy Added",
             "uom": "kWh", "device_class": "energy",
             "state_class": "total_increasing"},
            {"topic": "charger_power", "type": "sensor", "name": "Charger Power",
             "uom": "kW", "device_class": "power", "state_class": "measurement"},
            {"topic": "charger_voltage", "type": "sensor", "name": "Charger Voltage",
             "uom": "V", "device_class": "voltage", "state_class": "measurement"},
            {"topic": "charger_actual_current", "type": "sensor",
             "name": "Charger Current", "uom": "A", "device_class": "current",
             "state_class": "measurement"},
            {"topic": "charger_phases", "type": "sensor", "name": "Charger Phases",
             "icon": "mdi:sine-wave"},
            {"topic": "charge_current_request", "type": "sensor",
             "name": "Charge Current Request", "uom": "A", "device_class": "current",
             "state_class": "measurement"},
            {"topic": "charge_current_request_max", "type": "sensor",
             "name": "Max Charge Current", "uom": "A", "device_class": "current",
             "state_class": "measurement"},
            # --- TPMS ---
            {"topic": "tpms_pressure_fl", "type": "sensor",
             "name": "Tire Pressure FL", "uom": "bar", "device_class": "pressure",
             "state_class": "measurement"},
            {"topic": "tpms_pressure_fr", "type": "sensor",
             "name": "Tire Pressure FR", "uom": "bar", "device_class": "pressure",
             "state_class": "measurement"},
            {"topic": "tpms_pressure_rl", "type": "sensor",
             "name": "Tire Pressure RL", "uom": "bar", "device_class": "pressure",
             "state_class": "measurement"},
            {"topic": "tpms_pressure_rr", "type": "sensor",
             "name": "Tire Pressure RR", "uom": "bar", "device_class": "pressure",
             "state_class": "measurement"},
            # --- Binary Sensors ---
            {"topic": "plugged_in", "type": "binary_sensor", "name": "Plugged In",
             "device_class": "plug"},
            {"topic": "locked", "type": "binary_sensor", "name": "Locked",
             "device_class": "lock"},
            {"topic": "sentry_mode", "type": "binary_sensor", "name": "Sentry Mode",
             "icon": "mdi:shield-car"},
            {"topic": "windows_open", "type": "binary_sensor", "name": "Windows",
             "device_class": "window"},
            {"topic": "doors_open", "type": "binary_sensor", "name": "Doors",
             "device_class": "door"},
            {"topic": "charge_port_door_open", "type": "binary_sensor",
             "name": "Charge Port Door", "device_class": "door"},
            {"topic": "is_climate_on", "type": "binary_sensor", "name": "Climate",
             "device_class": "running"},
            {"topic": "is_preconditioning", "type": "binary_sensor",
             "name": "Preconditioning", "device_class": "heat"},
            {"topic": "update_available", "type": "binary_sensor",
             "name": "Update Available", "device_class": "update"},
            {"topic": "tpms_soft_warning_fl", "type": "binary_sensor",
             "name": "Tire Warning FL", "device_class": "problem"},
            {"topic": "tpms_soft_warning_fr", "type": "binary_sensor",
             "name": "Tire Warning FR", "device_class": "problem"},
            {"topic": "tpms_soft_warning_rl", "type": "binary_sensor",
             "name": "Tire Warning RL", "device_class": "problem"},
            {"topic": "tpms_soft_warning_rr", "type": "binary_sensor",
             "name": "Tire Warning RR", "device_class": "problem"},
        ]

        # Battery level is published first to register the full device block in HA
        self.mqtt_publish(
            f"homeassistant/sensor/{self.vin}/battery_level/config",
            json.dumps(
                {
                    "name": "Battery Level",
                    "state_topic": f"{self.basetopic}/battery_level",
                    "unique_id": f"{self.vin}_battery_level",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                    "state_class": "measurement",
                    "device": device_full,
                    "origin": ORIGIN,
                }
            ),
            retain=True,
        )

        for entry in ENTITIES:
            topic = entry["topic"]
            hasstype = entry["type"]
            data = {
                "name": entry["name"],
                "state_topic": f"{self.basetopic}/{topic}",
                "unique_id": f"{self.vin}_{topic}",
                "device": device_ref,
                "origin": ORIGIN,
            }
            if entry.get("uom"):
                data["unit_of_measurement"] = entry["uom"]
            if entry.get("device_class"):
                data["device_class"] = entry["device_class"]
            if entry.get("icon"):
                data["icon"] = entry["icon"]
            if entry.get("state_class"):
                data["state_class"] = entry["state_class"]
            if entry.get("entity_category"):
                data["entity_category"] = entry["entity_category"]

            self.mqtt_publish(
                f"homeassistant/{hasstype}/{self.vin}/{topic}/config",
                json.dumps(data),
                retain=True,
            )

        # Charge limit — number entity with get/set
        self.mqtt_publish(
            f"homeassistant/number/{self.vin}/charge_limit_soc/config",
            json.dumps(
                {
                    "name": "Charge Limit",
                    "state_topic": f"{self.basetopic}/charge_limit_soc",
                    "command_topic": f"{self.basetopic}/charge_limit_soc/set",
                    "unique_id": f"{self.vin}_charge_limit_soc",
                    "min": 50,
                    "max": 100,
                    "device": device_ref,
                    "icon": "mdi:battery-alert",
                    "origin": ORIGIN,
                }
            ),
            retain=True,
        )

        # Charging switch — start/stop charging
        self.mqtt_publish(
            f"homeassistant/switch/{self.vin}/charging/config",
            json.dumps(
                {
                    "name": "Charging",
                    "state_topic": f"{self.basetopic}/charging",
                    "command_topic": f"{self.basetopic}/charging/set",
                    "unique_id": f"{self.vin}_charging",
                    "device": device_ref,
                    "icon": "mdi:battery-charging",
                    "origin": ORIGIN,
                }
            ),
            retain=True,
        )

        # Device tracker — GPS location
        self.mqtt_publish(
            f"homeassistant/device_tracker/{self.vin}/gps/config",
            json.dumps(
                {
                    "name": "Location",
                    "json_attributes_topic": f"{self.basetopic}/gps",
                    "state_topic": f"{self.basetopic}/gps",
                    "value_template": "{{value_json.state}}",
                    "unique_id": f"{self.vin}_gps",
                    "device": device_ref,
                    "source_type": "gps",
                    "icon": "mdi:crosshairs-gps",
                    "origin": ORIGIN,
                }
            ),
            retain=True,
        )


def forcefloat(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0


def forceint(v):
    return int(forcefloat(v))


def main():
    t = TeslaBuddy()
    t.start()


if __name__ == "__main__":
    main()
