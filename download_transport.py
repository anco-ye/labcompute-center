"""Allow slow result receivers without weakening request/header timeouts."""
import re


def configure(server, write_timeout=300):
    """Use a bounded longer socket timeout only while sending result responses."""
    gateway = server.gateway

    class ResultGateway(gateway):
        def respond(self):
            result = (self.env.get('REQUEST_METHOD') == 'GET' and
                      re.fullmatch(r'/api/(?:jobs|batches)/[a-f0-9]{32}/result',
                                   self.env.get('PATH_INFO', '')))
            if not result:
                return super().respond()
            connection = self.req.conn.socket
            previous = connection.gettimeout()
            connection.settimeout(write_timeout)
            try:
                return super().respond()
            finally:
                # Keep-alive requests must retain the ordinary admission timeout.
                connection.settimeout(previous)

    server.gateway = ResultGateway
