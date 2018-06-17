import os
import logging
import xml.etree.ElementTree as ET
import re
import datetime
import urllib.request
import tornado.ioloop
import tornado.web
import tornado.httpclient
import xmltodict
import json
from expiringfilecache import ExpiringFileCache as FileCache
import copy

LOG_FORMAT = "%(asctime)s %(name)s[%(process)d] %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("InfinityProxy")

# Disable Tornado's access logging - we'll output ourselves
hn = logging.NullHandler()
hn.setLevel(logging.DEBUG)
logging.getLogger("tornado.access").addHandler(hn)
logging.getLogger("tornado.access").propagate = False

DEFAULT_STATEDIR = "./state"
DEFAULT_PORT = 3000
DEFAULT_LOGLEVEL = "DEBUG"
DEFAULT_PASSTHRU = 1

FILE_SYSTEM = "system.xml"
FILE_STATUS = "status.xml"
FILE_LOCAL_CHANGEFLAG = "local_changes"
FILE_REMOTE_CHANGEFLAG = "remote_changes"
TYPE_XML = "text/xml"
TYPE_JSON = "application/json"
TYPE_TEXT = "text/plain"

class InfinityProxy:

    def __init__(self, wundergroundApiKey="", port=DEFAULT_PORT, logLevel=DEFAULT_LOGLEVEL, stateDir=DEFAULT_STATEDIR, passthroughInterval=DEFAULT_PASSTHRU):
        logger.setLevel(logLevel)
        os.makedirs(stateDir, exist_ok=True)

        logger.info("Starting the Infinity proxy service on port %s", port)
        appSettings = {
            "wundergroundApiKey" : str(wundergroundApiKey),
            "stateDirectory" : stateDir,
            "passthroughInterval" : int(passthroughInterval)
        }
        app = self.getTornadoApp(appSettings)
        app.listen(int(port))
        try:
            tornado.ioloop.IOLoop.current().start()
        except KeyboardInterrupt:
            logger.info("Interrupt received, stopping the Infinity proxy service")
            tornado.ioloop.IOLoop.current().stop()

    def getTornadoApp(self, appSettings):
        return tornado.web.Application([
            #######################################################################
            # URI's corresponding to the Infinity thermostat protocol
            #######################################################################
            (r"/systems/(?P<systemID>\w*)/status", StatusUpdateHandler, {"action" : "status"}),
            (r"/systems/(?P<systemID>\w*)/config", ConfigRequestHandler, {"action" : "config"}),
            (r"/systems/(?P<systemID>\w*)/notifications", NotificationUpdateHandler, {"action" : "notifications"}),
            (r"/systems/(?P<systemID>\w*)/profile", LocalSaveHandler, {"action" : "profile"}),
            (r"/systems/(?P<systemID>\w*)/dealer", LocalSaveHandler, {"action" : "dealer"}),
            (r"/systems/(?P<systemID>\w*)/idu_config", LocalSaveHandler, {"action" : "idu_config"}),
            (r"/systems/(?P<systemID>\w*)/odu_config", LocalSaveHandler, {"action" : "odu_config"}),
            (r"/systems/(?P<systemID>\w*)/utility_events", LocalSaveHandler, {"action" : "utility_events"}),
            (r"/systems/(?P<systemID>\w*)/energy", LocalSaveHandler, {"action" : "energy"}),
            (r"/systems/(?P<systemID>\w*)/idu_status", LocalSaveHandler, {"action" : "idu_status"}),
            (r"/systems/(?P<systemID>\w*)/odu_status", LocalSaveHandler, {"action" : "odu_status"}),
            (r"/systems/(?P<systemID>\w*)/idu_faults", LocalSaveHandler, {"action" : "idu_faults"}),
            (r"/systems/(?P<systemID>\w*)/odu_faults", LocalSaveHandler, {"action" : "odu_faults"}),
            (r"/systems/(?P<systemID>\w*)/equipment_events", LocalSaveHandler, {"action" : "equipment_events"}),
            (r"/systems/(?P<systemID>\w*)/history", LocalSaveHandler, {"action" : "history"}),
            (r"/systems/(?P<systemID>\w*)/root_cause", LocalSaveHandler, {"action" : "root_cause"}),
            (r"/systems/(?P<systemID>\w*)", ConfigUpdateHandler, {"action" : "system"}),
            (r"/Alive", AliveHandler, {"action" : "Alive"}),
            (r"/weather/(?P<zipCode>.\w*)/forecast", WeatherUndergroundHandler, {"action" : "forecast"}),

            #######################################################################
            # API for external apps to interface with the thermostat
            #######################################################################
            (r"/api/(?P<fileName>system|status)\.?(?P<format>\w*)?/?", APIHandler),
            # Special config handlers for lists contained in system.xml
            (r"/api/config/wholeHouse/activities/activity/(?P<activityID>home|away|sleep|wake|manual)(?P<drilldownPath>.*)", APIHandler, {"xpathName": "WholeHouseActivity"}),
            (r"/api/config/zones/(?P<optionalZone>zone/)?(?P<zoneID>[1-8])/activities/(?P<optionalActivity>activity/)?(?P<activityID>home|away|sleep|wake|manual)(?P<drilldownPath>.*)", APIHandler, {"xpathName": "ZoneActivity"}),
            (r"/api/config/zones/(?P<OPTIONAL1>zone/)?(?P<zoneID>[1-8])/program/(?P<OPTIONAL2>day/)?(?P<dayID>Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)/(?P<OPTIONAL3>period/)?(?P<periodID>[1-5])(?P<drilldownPath>.*)", APIHandler, {"xpathName": "ZoneProgramPeriod"}),
            (r"/api/config/zones/(?P<OPTIONAL1>zone/)?(?P<zoneID>[1-8])/program/(?P<OPTIONAL2>day/)?(?P<dayID>Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)(?P<drilldownPath>.*)", APIHandler, {"xpathName": "ZoneProgram"}),
            (r"/api/config/zones/(?P<OPTIONAL1>zone/)?(?P<zoneID>[1-8])(?P<drilldownPath>.*)", APIHandler, {"xpathName": "Zone"}),
            # Default config handler for system.xml
            (r"/api/config(?P<drilldownPath>.*)", APIHandler, {"xpathName": "Config"}),

            #######################################################################
            # Catch-all for API calls we don't handle
            #######################################################################
            (r"/api/.*", APIHandler, {"action": "APINotImplemented"}),

            #######################################################################
            # Catch-all for anything else
            #######################################################################
            (r"/.*", DefaultHandler, {"action": "RequestNotImplemented"})

        ], **appSettings)


class BaseHandler(tornado.web.RequestHandler):
    """Provide common attributes and methods for all requests."""

    def initialize(self, **kwargs):
        tornado.web.RequestHandler.initialize(self)
        self.handlerConfig = kwargs
        self.stateDirectory = self.application.settings.get("stateDirectory")
        self.systemPath = os.path.join(self.stateDirectory, FILE_SYSTEM)
        self.statusPath = os.path.join(self.stateDirectory, FILE_STATUS)
        self.localChangePath = os.path.join(self.stateDirectory, FILE_LOCAL_CHANGEFLAG)
        self.action = self.handlerConfig.get("action", None)
        self.remoteChangePath = os.path.join(self.stateDirectory, FILE_REMOTE_CHANGEFLAG)
        self.passthroughInterval = self.application.settings.get("passthroughInterval")
        logger.debug("%s: %s", self.request.method, self.request.uri)

        # Skip if passthrough is not enabled, or this is a local api call, or we have pending local changes
        if self.passthroughInterval <= 0 or not any(s in self.request.host for s in ["carrier", "bryant"]) or self.isLocalConfigChange():
            logger.debug("Skipping passthrough")

        # If Carrier indicated pending changes on the last Status response,
        # or the requested file is not locally cached, proxy this request on to Carrier
        if self.isRemoteConfigChange() or not FileCache.exists(os.path.join(self.stateDirectory, self.action), ignoreExt=True):
            logger.debug("Passing through the request")
            request = copy.copy(self.request)
            request.protocol = "https"
            response = yield tornado.httpclient.AsyncHTTPClient().fetch(request)
            logger.debug(response.body)


    def writeDataToFile(self, data, filePath, secondsToLive=None):
        try:
            FileCache.write(filePath, data, secondsToLive)
        except Exception as e:
            logger.error("Failed to write '%s': %s'", filePath, e)
            raise e

    def readDataFromFile(self, filePath):
        try:
            data = FileCache.read(filePath)
        except Exception as e:
            logger.error("Failed to read '%s': %s'", filePath, e)
            return ""
        return data

    def isLocalConfigChange(self):
        return FileCache.exists(self.localChangePath)

    def setLocalConfigChange(self, enabled):
        if bool(enabled):
            FileCache.write(self.localChangePath, b"true")
        else:
            FileCache.remove(self.localChangePath)

    def isRemoteConfigChange(self):
        return FileCache.exists(self.remoteChangePath)

    def setRemoteConfigChange(self, enabled):
        if bool(enabled):
            FileCache.write(self.remoteChangePath, b"true")
        else:
            FileCache.remove(self.remoteChangePath)

    def formatOutgoingXml(self, rootElement):
        xmlString = ET.tostring(rootElement)
        xmlString = xmlString.replace(b" />", b"/>")  # Remove the extra space that ElementTree inserts in tag name of empty elements
        xmlString = re.sub(b"(\\s\\s+)|\\n", b"", xmlString)  # Strip space/tab formatting
        return xmlString

    def writeResponse(self, data, outputType=TYPE_XML):
        response = ""

        if isinstance(data, ET.Element):
            xmlString = self.formatOutgoingXml(data)
            if outputType == TYPE_XML:
                response = xmlString
            elif outputType == TYPE_JSON:
                response = json.dumps(xmltodict.parse(xmlString))
            else:
                response = ET.tostring(data)
        elif isinstance(data, dict):
            jsonString = json.dumps(data)
            if outputType == TYPE_JSON:
                response = jsonString
            elif outputType == TYPE_XML:
                response = xmltodict.unparse(data)
        else:
            outputType = TYPE_TEXT
            if data is None:
                data = ""
            response = str(data)
        self.set_header('Content-Type', outputType)
        self.write(response)


class ConfigUpdateHandler(BaseHandler):
    """Process configuration changes that were initiated on the thermostat."""

    def post(self, systemID):
        """Write POSTed thermostat configuration to the state directory."""

        data = self.request.arguments["data"][0]
        self.writeDataToFile(data, self.systemPath)


class StatusUpdateHandler(BaseHandler):
    """Process status updates generated by the thermostat."""

    def post(self, systemID):
        """Write POSTed XML thermostat state to the state directory, and generate a response."""

        if "data" not in self.request.arguments:
            return
        data = self.request.arguments["data"][0]
        self.writeDataToFile(data, self.statusPath)

        elementTextUpdates = {"timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
        if self.isLocalConfigChange():
            elementTextUpdates.update({"serverHasChanges": "true", "configHasChanges": "true"})
        responseXml = self.generateResponseXml(systemID, elementTextUpdates)
        self.writeResponse(responseXml, TYPE_XML)

    def generateResponseXml(self, systemID, elementTextUpdates={}, hostname="www.api.ing.carrier.com"):

        # This can be copy/pasted from an actual response in the event of future updates to the protocol
        sampleXML = """
            <status version="1.29" xmlns:atom="http://www.w3.org/2005/Atom">
                <atom:link rel="self" href="http://www.api.ing.carrier.com/systems/[[systemID]]/status" />
                <atom:link rel="http://www.api.ing.carrier.com/rels/system" href="http://www.api.ing.carrier.com/systems/[[systemID]]" />
                <timestamp>2018-01-01T00:00:00Z</timestamp>
                <pingRate>60</pingRate>
                <iduStatusPingRate>93600</iduStatusPingRate>
                <iduFaultsPingRate>86400</iduFaultsPingRate>
                <oduStatusPingRate>90000</oduStatusPingRate>
                <oduFaultsPingRate>82800</oduFaultsPingRate>
                <historyPingRate>75600</historyPingRate>
                <equipEventsPingRate>79200</equipEventsPingRate>
                <rootCausePingRate>72000</rootCausePingRate>
                <serverHasChanges>false</serverHasChanges>
                <configHasChanges>false</configHasChanges>
                <dealerHasChanges>false</dealerHasChanges>
                <dealerLogoHasChanges>false</dealerLogoHasChanges>
                <oduConfigHasChanges>false</oduConfigHasChanges>
                <iduConfigHasChanges>false</iduConfigHasChanges>
                <utilityEventsHasChanges>false</utilityEventsHasChanges>
            </status>
        """

        # Set up the Atom namespace for overriding the URLs and properly serializing the output
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        ET.register_namespace("atom", namespace["atom"])
        root = ET.fromstring(sampleXML)

        # Update the hostname URLS
        atomLinks = root.findall("./atom:link", namespace)
        for atomLink in atomLinks:
            if atomLink.attrib["rel"] == "self":
                atomLink.attrib["href"] = "http://{}/systems/{}/status".format(hostname, systemID)
            else:
                atomLink.attrib["rel"] = "http://{}/rels/system".format(hostname)
                atomLink.attrib["href"] = "http://{}/systems/{}".format(hostname, systemID)

        # Process any element updates
        for key, value in elementTextUpdates.items():
            elements = root.findall("./{}".format(key))
            for element in elements:
                element.text = str(value)
        return root


class ConfigRequestHandler(BaseHandler):
    """Send the local system config file back to the thermostat after it has been notified of a config change."""

    def get(self, systemID, hostname="www.api.ing.carrier.com", version="1.29"):
        """Reply with the local system XML, updated with ATOM elements and a current timestamp."""

        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        ET.register_namespace("atom", namespace["atom"])
        root = ET.parse(self.systemPath).getroot()
        configElement = root.find("./config")
        configElement.set("version", str(version))
        atomSelfElement = ET.Element("{%s}link" % namespace["atom"], {"rel": "self", "href": "http://%s/systems/%s/config" % (hostname, systemID)})
        atomHrefElement = ET.Element("{%s}link" % namespace["atom"], {"rel": "http://%s/rels/system" % (hostname), "href": "http://%s/systems/%s" % (hostname, systemID)})
        timestampElement = ET.Element("timestamp")
        timestampElement.text = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        configElement.insert(0, timestampElement)
        configElement.insert(0, atomHrefElement)
        configElement.insert(0, atomSelfElement)
        self.writeResponse(root, TYPE_XML)
        self.setLocalConfigChange(False)


class NotificationUpdateHandler(BaseHandler):
    """Process notification messages sent after the thermostat was updated."""

    def post(self, systemID):
        data = self.request.arguments["data"][0]
        root = ET.fromstring(data)
        notificationType = root.find("./notification/type").text
        code = root.find("./notification/code").text
        message = root.find("./notification/message").text
        timestamp = root.find("./notification/timestamp").text

        if code == "200":
            logger.info("Received %s notification: (%s) %s at %s", notificationType, code, message, timestamp)
        else:
            logger.error("Received %s notification: (%s) %s at %s", notificationType, code, message, timestamp)

        changes = []
        changeElements = root.findall("./notification/changes/change")
        for changeElement in changeElements:
            change = {"attributes": changeElement.attrib, "text": changeElement.text}
            changes.append(change)
            logger.debug("Change processed: %s", change)


class AliveHandler(BaseHandler):
    """Respond to periodic checks by the thermostat to confirm if the server is alive."""

    def get(self):
        self.write("alive")


class LocalSaveHandler(BaseHandler):
    """Save POST requests to the local state directory."""

    def post(self, systemID):
        data = self.request.arguments["data"][0]
        fileName = "{}.xml".format(os.path.basename(self.request.uri))
        filePath = os.path.join(self.stateDirectory, fileName)
        self.writeDataToFile(data, filePath)


class WeatherUndergroundHandler(BaseHandler):
    """Reply to weather forecast requests by transforming data from Weather Underground."""

    def get(self, zipCode, hostName="www.api.ing.carrier.com", version="1.29", ping=240):
        apiKey = self.application.settings.get("wundergroundApiKey")
        if len(apiKey) == 0:
            logger.warning("No Weather Underground API key provided.  Aborting request for forecast.")
            self.set_status(500, "No Weather Underground API key provided")
            self.write("Error retrieving forecast: No Weather Underground API key provided")
            return

        forecastUrl = "http://api.wunderground.com/api/{}/forecast10day/q/{}.xml".format(apiKey, zipCode)
        wuXmlString = urllib.request.urlopen(forecastUrl).read()
        wuForecasts = ET.fromstring(wuXmlString).findall("./forecast/simpleforecast/forecastdays/forecastday")

        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        ET.register_namespace("atom", namespace["atom"])
        forecastElement = ET.Element("weather_forecast", {"version": version})
        forecastElement.append(ET.Element("{%s}link" % namespace["atom"], {"rel": "self", "href": "http://%s/weather/%s/forecast" % (hostName, zipCode)}))
        forecastElement.append(ET.Element("{%s}link" % namespace["atom"], {"rel": "http://%s/rels/weather" % (hostName), "href": "http://%s/weather/%s" % (hostName, zipCode)}))
        forecastElement.append(self.createElement("timestamp", text=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")))
        forecastElement.append(self.createElement("ping", text=str(ping)))

        now = datetime.datetime.now()
        startDate = datetime.datetime(now.year, now.month, now.day).astimezone()  # Midnight today, with system-local timezone
        for dayNum in range(0, 6):
            forecastDate = startDate + datetime.timedelta(days=dayNum)
            dayName = forecastDate.strftime("%A")
            timestamp = forecastDate.isoformat()
            minTemp = wuForecasts[dayNum].find("./low/fahrenheit").text
            maxTemp = wuForecasts[dayNum].find("./high/fahrenheit").text
            statusID, statusMessage = self.iconToStatus(wuForecasts[dayNum].find("./icon").text)
            pop = wuForecasts[dayNum].find("./pop").text

            dayElement = ET.Element("day", {"id": dayName})
            dayElement.append(self.createElement("timestamp", text=timestamp))
            dayElement.append(self.createElement("min_temp", {"units": "f"}, text=minTemp))
            dayElement.append(self.createElement("max_temp", {"units": "f"}, text=maxTemp))
            dayElement.append(self.createElement("status_id", text=statusID))
            dayElement.append(self.createElement("status_message", text=statusMessage))
            dayElement.append(self.createElement("pop", text=pop))
            forecastElement.append(dayElement)

        self.writeResponse(forecastElement, TYPE_XML)

    def createElement(self, name, attrib={}, text=""):
        element = ET.Element(name, attrib)
        element.text = str(text)
        return element

    def iconToStatus(self, wuIcon):
        thermoConditions = ["Thunderstorms", "Sleet", "Rain and Sleet", "Wintry Mix", "Rain and Snow",
                            "Snow", "Freezing Rain", "Rain", "Blizzard", "Fog", "Cloudy", "Partly Cloudy",
                            "Mostly Cloudy", "Sunny"]
        wuIconMap = {
            "chanceflurries": "Snow",
            "chancerain": "Rain",
            "chancesleet": "Sleet",
            "chancesnow": "Snow",
            "chancetstorms": "Thunderstorms",
            "clear": "Sunny",
            "cloudy": "Cloudy",
            "flurries": "Snow",
            "fog": "Fog",
            "hazy": "Partly Cloudy",
            "mostlycloudy": "Mostly Cloudy",
            "mostlysunny": "Sunny",
            "partlycloudy": "Partly Cloudy",
            "partlysunny": "Sunny",
            "sleet": "Sleet",
            "rain": "Rain",
            "snow": "Snow",
            "sunny": "Sunny",
            "tstorms": "Thunderstorms",
            "unknown": "Sunny"
        }
        mappedCondition = wuIconMap[wuIcon]
        return (thermoConditions.index(mappedCondition) + 1, mappedCondition)


class DefaultHandler(BaseHandler):
    """Fallback handler for anything not picked up elsewhere."""

    def get(self):
        logger.warning("Unhandled request: %s %s", self.request.method, self.request.path)

    def post(self):
        return self.get()


class APIHandler(BaseHandler):
    """REST API to retrieve and update the local system config and status data."""

    formatToType = {"xml": TYPE_XML, "json": TYPE_JSON}

    def getXpath(self, xpathName, replacements):

        xpathMap = {
            "Config": "./config{drilldownPath}",
            "Zone": "./config/zones/zone[@id='{zoneID}']{drilldownPath}",
            "ZoneActivity": "./config/zones/zone[@id='{zoneID}']/activities/activity[@id='{activityID}']{drilldownPath}",
            "ZoneProgram": "./config/zones/zone[@id='{zoneID}']/program/day[@id='{dayID}']{drilldownPath}",
            "ZoneProgramPeriod": "./config/zones/zone[@id='{zoneID}']/program/day[@id='{dayID}']/period[@id='{periodID}']{drilldownPath}",
            "WholeHouseActivity": "./config/wholeHouse/activities/activity[@id='{activityID}']{drilldownPath}"
        }
        xpath = xpathMap[xpathName].format_map(replacements)
        return xpath

    def get(self, **pathVars):

        if self.handlerConfig.get("xpathName") is not None:
            root = ET.parse(self.systemPath).getroot()
            xpath = self.getXpath(self.handlerConfig.get("xpathName"), pathVars)
            if xpath is None or xpath == "":
                xpath = "."
            foundElement = root.find(xpath)
            self.writeResponse(foundElement, TYPE_JSON)
        else:
            outputFormat = self.formatToType.get(pathVars["format"], TYPE_JSON)
            fileNameNoExt = os.path.basename(self.request.uri).split(".")[0]
            filePath = "{}.xml".format(os.path.join(self.stateDirectory, fileNameNoExt))
            data = self.readDataFromFile(filePath)
            root_element = ET.fromstring(data)
            self.writeResponse(root_element, outputFormat)

    def post(self, **pathVars):

        updates = json.loads(self.request.body)
        xpath = self.getXpath(self.handlerConfig.get("xpathName"), pathVars)
        self.updateSystemConfig(xpath, updates)

    def updateSystemConfig(self, baseXpath, newValues):
        root = ET.parse(self.systemPath).getroot()
        for relativePath, newValue in newValues.items():
            fullXpath = "{}/{}".format(baseXpath, relativePath)
            foundElement = root.find(fullXpath)
            if foundElement is not None:
                if newValue is None or newValue == "null" or len(str(newValue)) == 0:
                    newValue = ""
                foundElement.text = str(newValue)
        xmlString = self.formatOutgoingXml(root)
        try:
            self.writeDataToFile(xmlString, self.systemPath)
            self.setLocalConfigChange(True)
        except Exception as e:
            logger.warning("Failed to update system config: %s", e)


if __name__ == "__main__":
    InfinityProxy()