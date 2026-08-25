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
import urllib.parse
import postgres

import paho.mqtt.client
import requests

# Number of retries to the Tesla API
COMMAND_RETRIES = 3
# Number of seconds between each retry attempt, scaled by 1.5 between each attempt
COMMAND_RETRY_DELAY = 10

# Number of seconds to cache the token from the TeslaMate DB
TOKEN_CACHE_TIME = 30

# How often the health file is touched, and the maximum age Docker should accept
HEALTH_FILE = os.environ.get("HEALTH_FILE", "/tmp/teslabuddy.healthy")
HEALTH_TOUCH_INTERVAL = 15

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s: %(levelname)s:%(name)s: %(message)s"
)
log = logging.getLogger(__name__)
logging.getLogger("paho.mqtt.client").setLevel(logging.INFO)
logging.getLogger("paho.mqtt").setLevel(logging.INFO)

# Included in every HA discovery message to identify this integration
ORIGIN = {
    "name": "teslabuddy",
    "support_url": "https://github.com/gummigroda/teslabuddy",
}

class TeslaBuddy:
    def __init__(self) -> None:
        self.config = self._initconfig()
        self.teslapiq = queue.Queue()
        self.teslamateq = queue.Queue()
        self._tokencache = {}

        self.tmid: int = -1
        self.eid: int = -1
        self.carname: str | None = None
        self.carmodeltxt: str | None = None
        self.error_sleep_time = COMMAND_RETRY_DELAY
        self.teslamatesettings = None

        self._starttime = time.time()
        self._mqttconnected = False
        self._stats = {"teslamate_msgs": 0, "published": 0, "commands": 0}
        self._lastteslamatemsg = 0.0

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
        log.info(
            "Connecting to TeslaMate database %s@%s:%s/%s",
            self.config.database_user,
            self.config.database_host,
            self.config.database_port,
            self.config.database_name,
        )
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

        self.statustopic = self.config.status_topic
        while self.statustopic.endswith("/"):
            self.statustopic = self.statustopic[:-1]
        self.statustopic += "/" + self.vin
        self.availabilitytopic = f"{self.statustopic}/availability"

        log.info(
            "TeslaMate database OK: car id %s, name %r, %s, VIN %s",
            self.tmid,
            self.carname,
            self.carmodeltxt,
            self.vin,
        )
        log.info("Publishing to base topic: %s", self.basetopic)
        log.info("Publishing status to: %s/status", self.statustopic)

    def getdbconn(self) -> postgres.Postgres:
        "Return a connection to the TeslaMate DB"
        dburl = (
            f"postgres://{urllib.parse.quote(self.config.database_user, safe='')}:"
            f"{urllib.parse.quote(self.config.database_pass, safe='')}@"
            f"{self.config.database_host}:{self.config.database_port}/{self.config.database_name}"
        )
        conn = postgres.Postgres(dburl)
        return conn

    def start(self):
        client_id = self.config.mqtt_client_id or "teslabuddy"
        self.client = paho.mqtt.client.Client(
            client_id=client_id,
            callback_api_version=paho.mqtt.client.CallbackAPIVersion.VERSION2,
        )
        self.client.on_connect = self.onmqttconnect
        self.client.on_disconnect = self.onmqttdisconnect
        self.client.on_message = self.onmqttmessage
        self.client.will_set(self.availabilitytopic, "offline", retain=True)

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

        log.info(
            "Connecting to MQTT broker %s:%s (TLS %s, auth %s)",
            self.config.mqtt_host,
            port,
            "enabled" if use_tls else "disabled",
            "enabled" if self.config.mqtt_user else "disabled",
        )
        self.client.connect(self.config.mqtt_host, port)
        self.client.loop_start()

        # Thread to manage waking TeslaMate when incoming commands happen
        threading.Thread(target=self.waketeslamatethread, daemon=True).start()
        # Thread to log periodic status and update the Docker health file
        threading.Thread(target=self.statusthread, daemon=True).start()

        self.homeassistantsetup()
        log.info("Home Assistant discovery configuration published")
        self.publishstatus()
        log.info(
            "Startup complete, status updates every %s seconds",
            self.config.status_interval,
        )
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

    def onmqttconnect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            self._mqttconnected = False
            reason_name = getattr(reason_code, "name", str(reason_code))
            reason_value = getattr(reason_code, "value", reason_code)
            try:
                error_text = paho.mqtt.client.error_string(reason_value)
            except AttributeError:
                error_text = str(reason_code)
            log.error(
                "MQTT connection failed: %s (%s) - %s",
                reason_name,
                reason_value,
                error_text,
            )
            return
        self._mqttconnected = True
        log.info("Connected to MQTT broker")
        self.client.publish(self.availabilitytopic, "online", retain=True)
        self.client.subscribe(f"{self.basetopic}/+/set")
        self.client.subscribe(f"teslamate/cars/{self.tmid}/+")
        for topic in self.wake_topics:
            self.client.subscribe(topic)
        log.info(
            "Subscribed to %s/+/set, teslamate/cars/%s/+ and %d wake topic(s)",
            self.basetopic,
            self.tmid,
            len(self.wake_topics),
        )

    def onmqttdisconnect(
        self, client, userdata, disconnect_flags, reason_code, properties=None
    ):
        self._mqttconnected = False
        log.warning("Disconnected from MQTT broker (%s), will retry", reason_code)

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
        self._stats["published"] += 1
        self.client.publish(topic, payload, retain=retain)

    def statusthread(self):
        """Periodically log a status summary, publish it to MQTT and touch the
        Docker health file

        The health file is only refreshed while MQTT is connected, so a broken
        connection eventually marks the container unhealthy.
        """
        lastlog = time.time()
        while 1:
            if self._mqttconnected:
                try:
                    with open(HEALTH_FILE, "w") as f:
                        f.write(str(int(time.time())))
                except OSError as e:
                    log.debug("Could not write health file %s: %s", HEALTH_FILE, e)

            interval = self.config.status_interval
            if interval > 0 and time.time() - lastlog >= interval:
                lastlog = time.time()
                self.publishstatus()
            time.sleep(HEALTH_TOUCH_INTERVAL)

    def publishstatus(self):
        "Log and publish the current status/statistics"
        uptime = time.time() - self._starttime
        if self._lastteslamatemsg:
            lastmsgage = round(time.time() - self._lastteslamatemsg)
            lastmsg = f"{lastmsgage}s ago"
        else:
            lastmsgage = None
            lastmsg = "never"
        log.info(
            "Status: uptime %s, MQTT %s, TeslaMate messages %d "
            "(last %s), published %d, Tesla API commands %d",
            formatduration(uptime),
            "connected" if self._mqttconnected else "DISCONNECTED",
            self._stats["teslamate_msgs"],
            lastmsg,
            self._stats["published"],
            self._stats["commands"],
        )
        self.mqtt_publish(
            f"{self.statustopic}/status",
            json.dumps(
                {
                    "state": "online",
                    "vin": self.vin,
                    "car_name": self.carname,
                    "teslamate_car_id": self.tmid,
                    "started": int(self._starttime),
                    "uptime_seconds": round(uptime),
                    "uptime": formatduration(uptime),
                    "teslamate_messages": self._stats["teslamate_msgs"],
                    "last_teslamate_message_seconds": lastmsgage,
                    "messages_published": self._stats["published"],
                    "tesla_api_commands": self._stats["commands"],
                    "timestamp": int(time.time()),
                }
            ),
            retain=True,
        )

    def teslamatemsg(self, topic, value):
        """Process a message from TeslaMate.

        TeslaMate remains the authoritative source for vehicle state.
        teslabuddy retains only its own command and status topics.
        """
        self._stats["teslamate_msgs"] += 1
        self._lastteslamatemsg = time.time()

        # Intentionally no state mirroring under the teslabuddy namespace.
        # Home Assistant discovery points directly to TeslaMate's live topics.

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
            "--mqtt-client-id",
            help="MQTT client id to use for the broker connection (default: teslabuddy)",
            default="teslabuddy",
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
            default="teslabuddy",
        )

        parser.add_argument(
            "--wake-topics",
            help="a space separated list of MQTT topics to subscribe to and use to "
            "wake up TeslaMate. If any of the topics are called with any value, "
            "the TeslaMate API will be called to cancel sleep. "
            "Used for example when the garage door is opened",
        )

        parser.add_argument(
            "--status-topic",
            help="base MQTT topic for teslabuddy status/statistics messages, "
            "no trailing / (default teslabuddy)",
            default="teslabuddy",
        )

        parser.add_argument(
            "--status-interval",
            help="seconds between periodic status log lines, 0 to disable "
            "(default 300)",
            default=300,
            type=int,
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
            logging.getLogger("paho.mqtt.client").setLevel(logging.DEBUG)
            logging.getLogger("paho.mqtt").setLevel(logging.DEBUG)
        if args.debug is not True:
            args.debug = False
            logging.getLogger("paho.mqtt.client").setLevel(logging.INFO)
            logging.getLogger("paho.mqtt").setLevel(logging.INFO)
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
                self._stats["commands"] += 1
                log.info("Tesla API command applied: %s = %s", key, value)
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

        teslamatetopic = f"teslamate/cars/{self.tmid}"

        # HA marks entities unavailable when TeslaMate reports the car unhealthy
        availability = {
            "availability_topic": f"{teslamatetopic}/healthy",
            "payload_available": "true",
            "payload_not_available": "false",
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
            {"topic": "update_version", "type": "sensor", "name": "Available Update",
             "entity_category": "diagnostic"},
            {"topic": "since", "type": "sensor", "name": "Since",
             "entity_category": "diagnostic", "icon": "mdi:clock"},
            {"topic": "healthy", "type": "sensor", "name": "Vehicle Health",
             "entity_category": "diagnostic", "icon": "mdi:heart-pulse"},
            {"topic": "center_display_state", "type": "sensor", "name": "Display State",
             "entity_category": "diagnostic", "icon": "mdi:monitor"},
            {"topic": "display_name", "type": "sensor", "name": "Display Name",
             "entity_category": "diagnostic", "icon": "mdi:label"},
            {"topic": "scheduled_charging_start_time", "type": "sensor",
             "name": "Scheduled Charge Time", "icon": "mdi:clock-outline"},
            # --- Driving Data ---
            {"topic": "power", "type": "sensor", "name": "Power",
             "uom": "W", "device_class": "power", "state_class": "measurement",
             "icon": "mdi:lightning-bolt"},
            {"topic": "speed", "type": "sensor", "name": "Speed",
             "uom": "km/h", "icon": "mdi:speedometer"},
            {"topic": "heading", "type": "sensor", "name": "Heading",
             "uom": "°", "icon": "mdi:compass"},
            {"topic": "elevation", "type": "sensor", "name": "Elevation",
             "uom": "m", "device_class": "distance", "icon": "mdi:mountain"},
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
            # --- Vehicle Info ---
            {"topic": "model", "type": "sensor", "name": "Model",
             "entity_category": "diagnostic", "icon": "mdi:information"},
            {"topic": "trim_badging", "type": "sensor", "name": "Trim",
             "entity_category": "diagnostic", "icon": "mdi:badge"},
            {"topic": "exterior_color", "type": "sensor", "name": "Exterior Color",
             "entity_category": "diagnostic", "icon": "mdi:palette"},
            {"topic": "wheel_type", "type": "sensor", "name": "Wheel Type",
             "entity_category": "diagnostic", "icon": "mdi:wheel"},
            {"topic": "spoiler_type", "type": "sensor", "name": "Spoiler Type",
             "entity_category": "diagnostic", "icon": "mdi:car-back"},
            # --- Sunroof ---
            {"topic": "sun_roof_installed", "type": "binary_sensor", "name": "Sunroof Installed",
             "entity_category": "diagnostic", "icon": "mdi:window-closed"},
            {"topic": "sun_roof_state", "type": "sensor", "name": "Sunroof State",
             "icon": "mdi:window-closed"},
            {"topic": "sun_roof_percent_open", "type": "sensor", "name": "Sunroof Open",
             "uom": "%", "icon": "mdi:window-open", "state_class": "measurement"},
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
            {"topic": "windows_open", "type": "binary_sensor", "name": "Windows Open",
             "device_class": "window"},
            {"topic": "driver_front_window_open", "type": "binary_sensor",
             "name": "Driver Front Window", "device_class": "window"},
            {"topic": "driver_rear_window_open", "type": "binary_sensor",
             "name": "Driver Rear Window", "device_class": "window"},
            {"topic": "passenger_front_window_open", "type": "binary_sensor",
             "name": "Passenger Front Window", "device_class": "window"},
            {"topic": "passenger_rear_window_open", "type": "binary_sensor",
             "name": "Passenger Rear Window", "device_class": "window"},
            {"topic": "doors_open", "type": "binary_sensor", "name": "Doors Open",
             "device_class": "door"},
            {"topic": "driver_front_door_open", "type": "binary_sensor",
             "name": "Driver Front Door", "device_class": "door"},
            {"topic": "driver_rear_door_open", "type": "binary_sensor",
             "name": "Driver Rear Door", "device_class": "door"},
            {"topic": "passenger_front_door_open", "type": "binary_sensor",
             "name": "Passenger Front Door", "device_class": "door"},
            {"topic": "passenger_rear_door_open", "type": "binary_sensor",
             "name": "Passenger Rear Door", "device_class": "door"},
            {"topic": "trunk_open", "type": "binary_sensor", "name": "Trunk Open",
             "device_class": "door", "icon": "mdi:car-back"},
            {"topic": "frunk_open", "type": "binary_sensor", "name": "Frunk Open",
             "device_class": "door", "icon": "mdi:car-front"},
            {"topic": "charge_port_door_open", "type": "binary_sensor",
             "name": "Charge Port Door", "device_class": "door"},
            {"topic": "is_climate_on", "type": "binary_sensor", "name": "Climate",
             "device_class": "running"},
            {"topic": "is_preconditioning", "type": "binary_sensor",
             "name": "Preconditioning", "device_class": "heat"},
            {"topic": "is_user_present", "type": "binary_sensor",
             "name": "User Present", "icon": "mdi:human-greeting-variant"},
            {"topic": "update_available", "type": "binary_sensor",
             "name": "Update Available", "device_class": "update"},
            {"topic": "service_mode", "type": "binary_sensor", "name": "Service Mode",
             "entity_category": "diagnostic", "icon": "mdi:tools"},
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
                    "state_topic": f"{teslamatetopic}/battery_level",
                    "unique_id": f"{self.vin}_battery_level",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                    "state_class": "measurement",
                    "device": device_full,
                    "origin": ORIGIN,
                    **availability,
                }
            ),
            retain=True,
        )

        for entry in ENTITIES:
            topic = entry["topic"]
            hasstype = entry["type"]
            data = {
                "name": entry["name"],
                "state_topic": f"{teslamatetopic}/{topic}",
                "unique_id": f"{self.vin}_{topic}",
                "device": device_ref,
                "origin": ORIGIN,
                **availability,
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

            if hasstype == "binary_sensor":
                data["payload_on"] = True
                data["payload_off"] = False
            elif hasstype == "switch":
                data["payload_on"] = True
                data["payload_off"] = False

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
                    "state_topic": f"{teslamatetopic}/charge_limit_soc",
                    "command_topic": f"{self.basetopic}/charge_limit_soc/set",
                    "unique_id": f"{self.vin}_charge_limit_soc",
                    "min": 50,
                    "max": 100,
                    "device": device_ref,
                    "icon": "mdi:battery-alert",
                    "origin": ORIGIN,
                    **availability,
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
                    "state_topic": f"{teslamatetopic}/charging",
                    "command_topic": f"{self.basetopic}/charging/set",
                    "payload_on": True,
                    "payload_off": False,
                    "unique_id": f"{self.vin}_charging",
                    "device": device_ref,
                    "icon": "mdi:battery-charging",
                    "origin": ORIGIN,
                    **availability,
                }
            ),
            retain=True,
        )

        # Device tracker — GPS location. Home Assistant expects the state to come
        # from the geofence/home topic and the coordinates from the location JSON.
        # TeslaMate publishes both of these directly, so discovery should point to
        # the live TeslaMate topics instead of a teslabuddy-only derived payload.
        self.mqtt_publish(
            f"homeassistant/device_tracker/{self.vin}/config",
            json.dumps(
                {
                    "name": "Location",
                    "state_topic": f"{teslamatetopic}/geofence",
                    "json_attributes_topic": f"{teslamatetopic}/location",
                    "value_template": "{{ value | lower }}",
                    "payload_home": "home",
                    "payload_not_home": "not_home",
                    "unique_id": f"{self.vin}_gps",
                    "device": device_ref,
                    "source_type": "gps",
                    "icon": "mdi:crosshairs-gps",
                    "origin": ORIGIN,
                    **availability,
                }
            ),
            retain=True,
        )

        # teslabuddy's own status, exposed as a diagnostic entity
        self.mqtt_publish(
            f"homeassistant/sensor/{self.vin}/teslabuddy_status/config",
            json.dumps(
                {
                    "name": "TeslaBuddy Uptime",
                    "state_topic": f"{self.statustopic}/status",
                    "json_attributes_topic": f"{self.statustopic}/status",
                    "value_template": "{{value_json.uptime}}",
                    "unique_id": f"{self.vin}_teslabuddy_status",
                    "device": device_ref,
                    "entity_category": "diagnostic",
                    "icon": "mdi:heart-pulse",
                    "origin": ORIGIN,
                    **availability,
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


def formatduration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


def main():
    log.info("Starting teslabuddy")
    t = TeslaBuddy()
    t.start()


if __name__ == "__main__":
    main()
