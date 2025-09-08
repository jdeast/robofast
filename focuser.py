
class Focuser:

    def __init__(self, config):
        pass

    def move(self, position):

        # make sure it's a legal move first
        if position < float(self.minfocus) or position > float(self.maxfocus):
            self.logger.warning('Requested focus (' + str(position) + ') out of bounds')
            return False

    def moveAndWait(self, position):
        pass

    def move_absolute(self, position):
        pass

    def move_relative(self, position):
        pass

