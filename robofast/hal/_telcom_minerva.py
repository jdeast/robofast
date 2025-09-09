# class to facilitate communications with the telescope
class Telcom:

    def __init__(self, config):
        pass

    # SUPPORT FUNCTIONS
    def makeUrl(self, **kwargs):
        """
        Utility function that takes a set of keyword=value arguments
        and converts them into a properly formatted URL to send to PWI.
        For example, calling the function as:
          makeUrl(device="mount", cmd="move", ra2000="10 20 30", dec2000="20 30 40")
        will return the string:
          http://127.0.0.1:8080/?device=mount&cmd=move&dec=20+30+40&ra=10+20+30

        Note that spaces have been URL-encoded to "+" characters.
        """

        url = "http://" + self.HOST + ":" + str(self.NETWORKPORT) + "/?"
        url = url + urllib.urlencode(kwargs.items())
        return url

    def pwiRequest(self, **kwargs):
        """
        Issue a request to PWI using the keyword=value parameters
        supplied to the function, and return the response received from
        PWI.

        For example:
          makeUrl(device="mount", cmd="move", ra2000="10 20 30", dec2000="20 30 40")
        will request a slew to J2000 coordinates 10:20:30, 20:30:40, and will
        (under normal circumstances) return the status of the telescope as an
        XML string.
        """
        url = self.makeUrl(**kwargs)
        try: ret = urllib.urlopen(url).read()
        except: ret = False

        return ret

    def parseXml(self, xml):
        """
        Convert the XML into a smart structure that can be navigated via
        the tree of tag names; e.g. "status.mount.ra"
        """

        return elementTreeToObject(ElementTree.fromstring(xml))


    def pwiRequestAndParse(self, **kwargs):
        """
        Works like pwiRequest(), except returns a parsed XML object rather
        than XML text
        """
        ret = self.pwiRequest(**kwargs)
        if ret == False: return False
        return self.parseXml(ret)

    ### Status wrappers #####################################
    def getStatusXml(self):
        """
        Return a string containing the XML text representing the status of the telescope
        """
        return self.pwiRequest(cmd="getsystem")