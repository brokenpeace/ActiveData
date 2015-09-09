# encoding: utf-8
#
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from flask import Flask
import flask
from werkzeug.contrib.fixers import HeaderRewriterFix
from werkzeug.wrappers import Response

from active_data.save_query import SaveQueries
from pyLibrary import convert, strings
from pyLibrary.debugs import constants, startup
from pyLibrary.debugs.logs import Log, Except
from pyLibrary.dot import unwrap, wrap, coalesce
from pyLibrary.env import elasticsearch
from pyLibrary.env.files import File
from pyLibrary.queries import qb, meta
from pyLibrary.queries import containers
from pyLibrary.strings import expand_template
from pyLibrary.thread.threads import Thread
from pyLibrary.times.dates import Date
from pyLibrary.times.timer import Timer


OVERVIEW = File("active_data/html/index.html").read()
BLANK = File("active_data/html/error.html").read()

app = Flask(__name__)
request_log_queue = None
# default_elasticsearch = None
config = None
query_finder = None

def record_request(request, query_, data, error):
    if request_log_queue == None:
        return

    log = wrap({
        "timestamp": Date.now(),
        "http_user_agent": request.headers.get("user_agent"),
        "http_accept_encoding": request.headers.get("accept_encoding"),
        "path": request.headers.environ["werkzeug.request"].full_path,
        "content_length": request.headers.get("content_length"),
        "remote_addr": request.remote_addr,
        "query": query_,
        "data": data,
        "error": error
    })
    log["from"] = request.headers.get("from")
    request_log_queue.add({"value": log})


@app.route('/tools/<path:filename>')
def download_file(filename):
    return flask.send_from_directory(File("active_data/html").abspath, filename)

@app.route('/find/<path:hash>')
def find_query(hash):
    hash = hash.split("/")[0]
    query = query_finder.find(hash)

    if not query:
        return Response(
            b'{"type":"ERROR", "template":"not found"}',
            status=400,
            headers={
                "access-control-allow-origin": "*",
                "Content-type": "application/json"
            }
        )
    else:
        return Response(
            convert.unicode2utf8(query),
            status=200,
            headers={
                "access-control-allow-origin": "*",
                "Content-type": "application/json"
            }
        )

@app.route('/query', defaults={'path': ''}, methods=['GET', 'POST'])
def query(path):
    active_data_timer = Timer("total duration")
    try:
        with active_data_timer:
            body = flask.request.environ['body_copy']
            if not body.strip():
                return Response(
                    convert.unicode2utf8(BLANK),
                    status=400,
                    headers={
                        "access-control-allow-origin": "*",
                        "Content-type": "text/html"
                    }
                )

            text = convert.utf82unicode(body)
            text = replace_vars(text, flask.request.args)
            data = convert.json2value(text)
            record_request(flask.request, data, None, None)
            if data.meta.testing:
                # MARK ALL QUERIES FOR TESTING SO FULL METADATA IS AVAILABLE BEFORE QUERY EXECUTION
                m = meta.singlton
                cols = [c for c in m.get_columns(table=data["from"]) if c.type not in ["nested", "object"]]
                for c in cols:
                    m.todo.push(c)

                while True:
                    for c in cols:
                        if not c.last_updated:
                            Log.note("wait for column (table={{col.table}}, name={{col.name}}) metadata to arrive", col=c)
                            break
                    else:
                        break
                    Thread.sleep(seconds=1)
            # frum = Container.new_instance(data["from"])
            result = qb.run(data)

        if data.meta.save:
            result.meta.saved_as = query_finder.save(data)

        result.meta.active_data_response_time = active_data_timer.duration

        return Response(
            convert.unicode2utf8(convert.value2json(result)),
            direct_passthrough=True,  # FOR STREAMING
            status=200,
            headers={
                "access-control-allow-origin": "*",
                "Content-type": result.meta.content_type
            }
        )
    except Exception, e:
        e = Except.wrap(e)

        record_request(flask.request, None, flask.request.environ['body_copy'], e)
        Log.warning("Problem sent back to client", e)
        e = e.as_dict()
        e.meta.active_data_response_time = active_data_timer.duration

        return Response(
            convert.unicode2utf8(convert.value2json(e)),
            status=400,
            headers={
                "access-control-allow-origin": "*",
                "Content-type": "application/json"
            }
        )


@app.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
@app.route('/<path:path>')
def overview(path):
    record_request(flask.request, None, flask.request.environ['body_copy'], None)

    return Response(
        convert.unicode2utf8(OVERVIEW),
        status=400,
        headers={
            "access-control-allow-origin": "*",
            "Content-type": "text/html"
        }
    )


def replace_vars(text, params=None):
    """
    REPLACE {{vars}} WITH ENVIRONMENTAL VALUES
    """
    start = 0
    var = strings.between(text, "{{", "}}", start)
    while var:
        replace = "{{" + var + "}}"
        index = text.find(replace, 0)
        end = index + len(replace)

        try:
            replacement = unicode(Date(var).unix)
            text = text[:index] + replacement + text[end:]
            start = index + len(replacement)
        except Exception, _:
            start += 1

        var = strings.between(text, "{{", "}}", start)

    text = expand_template(text, coalesce(params, {}))
    return text


# Snagged from http://stackoverflow.com/questions/10999990/python-flask-how-to-get-whole-raw-post-body
class WSGICopyBody(object):
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        from cStringIO import StringIO

        length = environ.get('CONTENT_LENGTH', '0')
        length = 0 if length == '' else int(length)

        body = environ['wsgi.input'].read(length)
        environ['body_copy'] = body
        environ['wsgi.input'] = StringIO(body)

        # Call the wrapped application
        app_iter = self.application(environ, self._sr_callback(start_response))

        # Return modified response
        return app_iter

    def _sr_callback(self, start_response):
        def callback(status, headers, exc_info=None):
            # Call upstream start_response
            start_response(status, headers, exc_info)

        return callback


app.wsgi_app = WSGICopyBody(app.wsgi_app)


def main():
    # global default_elasticsearch
    global request_log_queue
    global config
    global query_finder

    try:
        config = startup.read_settings()
        constants.set(config.constants)
        Log.start(config.debug)

        # PIPE REQUEST LOGS TO ES DEBUG
        if config.request_logs:
            request_logger = elasticsearch.Cluster(config.request_logs).get_or_create_index(config.request_logs)
            request_log_queue = request_logger.threaded_queue(max_size=2000)

        # default_elasticsearch = elasticsearch.Cluster(config.elasticsearch)

        containers.config.default = {
            "type": "elasticsearch",
            "settings": config.elasticsearch.copy()
        }

        if config.saved_queries:
            query_finder = SaveQueries(config.saved_queries)

        HeaderRewriterFix(app, remove_headers=['Date', 'Server'])
        app.run(**unwrap(config.flask))
    except Exception, e:
        Log.error("Problem with etl", cause=e)
    finally:
        Log.stop()


if __name__ == "__main__":
    main()

