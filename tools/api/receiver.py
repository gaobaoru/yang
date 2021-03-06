import base64
import datetime
import errno
import json
import os
import shutil
import subprocess
import urllib2
from urllib2 import URLError

import pika
import sys

import tools.utility.log as log

LOGGER = log.get_logger('receiver')


# Make a http request on path with json_data
def http_request(path, method, json_data, http_credentials, header):
    try:
        opener = urllib2.build_opener(urllib2.HTTPHandler)
        request = urllib2.Request(path, data=json_data)
        request.add_header('Content-Type', header)
        request.add_header('Accept', header)
        base64string = base64.b64encode('%s:%s' % (http_credentials[0], http_credentials[1]))
        request.add_header("Authorization", "Basic %s" % base64string)
        request.get_method = lambda: method
        return opener.open(request)
    except urllib2.HTTPError as e:
        LOGGER.debug('Could not send request with body ' + repr(json_data) + ' and path ' + path)
        raise e
    except URLError as e:
        raise e


def process_sdo(arguments):
    LOGGER.debug('Processing sdo')
    tree_created = True if arguments[-3] == 'True' else False
    arguments = arguments[:-4]
    direc = '/'.join(arguments[6].split('/')[0:3])

    with open("log.txt", "wr") as f:
        try:
            subprocess.check_call(arguments, stderr=f)
        except subprocess.CalledProcessError as e:
            shutil.rmtree(direc)
            LOGGER.error('Server error: {}'.format(e))
            return __response_type[0] + '#split#Server error while parsing or populating data'

    try:
        os.makedirs('../../api/sdo/')
    except OSError as e:
        # be happy if someone already created the path
        if e.errno != errno.EEXIST:
            return __response_type[0] + '#split#Server error - could not create directory'

    if tree_created:
        subprocess.call(["cp", "-r", direc + "/temp/.", "../../api/sdo/"])
        send_to_indexing(direc + '/prepare.json',
                         [arguments[11], arguments[12]], arguments[9], True)
        shutil.rmtree(direc)
    return __response_type[1]


def send_to_indexing(file_to_index, credentials, confd_ip, sdo_type=False, body='modules-to-index'):
    LOGGER.debug('Sending data for indexing')
    with open(file_to_index, 'r') as f:
        sdos_json = json.load(f)
    post_body = {}
    if sdo_type:
        prefix = 'api/sdo'
    else:
        prefix = 'api/vendor'

    for sdo in sdos_json['module']:
        if sdo.get('schema'):
            path = prefix + sdo['schema'].split('githubusercontent.com/')[1]
        else:
            path = 'module does not exist'
        post_body[sdo['name'] + '@' + sdo['revision']] = path
    body_to_send = json.dumps({'modules-to-index': post_body})
    LOGGER.info('Sending data for indexing with body {}'.format(body_to_send))
    try:
        http_request('https://' + confd_ip + '/yang-search/metadata-update.php', 'POST', body_to_send,
                     credentials, 'application/json')
    except urllib2.HTTPError as e:
        LOGGER.error('could not send data for indexing. Reason: {}'.format(e.msg))
    except URLError as e:
        LOGGER.error('could not send data for indexing. Reason: {}'.format(repr(e.message)))


def process_vendor(arguments):
    LOGGER.debug('Processing vendor')
    tree_created = True if arguments[-5] == 'True' else False
    integrity_file_location = arguments[-4]

    arguments = arguments[:-5]
    direc = '/'.join(arguments[5].split('/')[0:3])

    with open("log.txt", "wr") as f:
        try:
            subprocess.check_call(arguments, stderr=f)
        except subprocess.CalledProcessError as e:
            shutil.rmtree(direc)
            LOGGER.error('Server error: {}'.format(e))
            return __response_type[0] + '#split#Server error while parsing or populating data'
    try:
        os.makedirs('../../api/vendor/')
    except OSError as e:
        # be happy if someone already created the path
        if e.errno != errno.EEXIST:
            LOGGER.error('Server error: {}'.format(e))
            return __response_type[0] + '#split#Server error - could not create directory'

    subprocess.call(["cp", "-r", direc + "/temp/.", "../../api/vendor/"])

    if tree_created:
        send_to_indexing(direc + '/prepare.json', [arguments[9], arguments[10]], arguments[8])
        shutil.rmtree(direc)

    integrity_file_name = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%m:%S.%f")[:-3] + 'Z'

    if integrity_file_location != './':
        shutil.move('./integrity.html', integrity_file_location + 'integrity' + integrity_file_name + '.html')
    return __response_type[1]


def process_vendor_deletion(arguments, api_protocol, api_port):
    vendor, platform, software_version, software_flavor = arguments[0:4]
    protocol, confd_ip, confdPort = arguments[4:7]
    credentials = arguments[7:9]
    path_to_delete = arguments[9]

    response = 'work'
    try:
        with open('./cache/catalog.json', 'r') as catalog:
            vendors_data = json.load(catalog)['yang-catalog:catalog']['vendors']
    except IOError:
        LOGGER.warning('Cache file does not exist')
        # Try to create a cache if not created yet and load data again
        response = make_cache(credentials, response, protocol, confd_ip, confdPort, api_protocol, api_port)
        if response != 'work':
            return response
        else:
            try:
                with open('./cache/catalog.json', 'r') as catalog:
                    vendors_data = json.load(catalog)['yang-catalog:catalog']['vendors']
            except:
                LOGGER.error('Unexpected error: {}'.format(sys.exc_info()[0]))
                return __response_type[0] + '#split#' + sys.exc_info()[0]

    modules = set()
    iterate_in_depth(vendors_data, modules)

    try:
        http_request(path_to_delete, 'DELETE', None, credentials, 'application/vnd.yang.data+json')
    except:
        e = sys.exc_info()[0]
        LOGGER.error('Couldn\'t delete vendor on path {}. Error: '.format(path_to_delete, e))
        return __response_type[0] + '#split#' + repr(e)

    for mod in modules:
        try:
            path = protocol + '://' + confd_ip + ':' + repr(confdPort) + '/api/config/catalog/modules/module/' \
                   + mod
            modules_data = json.loads(http_request(path + '?deep', 'GET', '', credentials,
                                                   'application/vnd.yang.data+json').read())
            implementations = modules_data['yang-catalog:module']['implementations']['implementation']
            count_of_implementations = len(implementations)
            count_deleted = 0
            for imp in implementations:
                if vendor and vendor != imp['vendor']:
                    continue
                if platform and platform != imp['platform']:
                    continue
                if software_flavor and software_flavor != imp['software-flavor']:
                    continue
                if software_version and software_version != imp['software-version']:
                    continue
                imp_key = imp['vendor'] + ',' + imp['platform'] + ',' + imp['software-version'] \
                          + ',' + imp['software-flavor']
                try:
                    http_request(path + '/implementations/implementation/' + imp_key,
                                 'DELETE', None, credentials, 'application/vnd.yang.data+json')
                except:
                    e = sys.exc_info()[0]
                    LOGGER.error('Couldn\'t delete implementation of module on path {} because of error: {}'
                                 .format(path + '/implementations/implementation/' + imp_key, e))
                    # return make_response(jsonify({'error': e.msg}), 500)
                count_deleted += 1

            if count_deleted == count_of_implementations:
                try:
                    http_request(path, 'DELETE', None, credentials, 'application/vnd.yang.data+json')
                except:
                    e = sys.exc_info()[0]
                    LOGGER.error('Could not delete module on path {} because of error: {}'.format(path, e))
                    # return make_response(jsonify({'error': e.msg}), 500)
        except:
            LOGGER.error('Yang file {} doesn\'t exist although it should exist'.format(mod))
            # return make_response(jsonify({'error': 'Server error'}), 500)
        return __response_type[1]


def iterate_in_depth(value, modules):
    for key, val in value.iteritems():
        if key == 'protocols':
            continue
        if isinstance(val, list):
            for v in val:
                iterate_in_depth(v, modules)
        if isinstance(val, dict):
            if key == 'modules':
                for mod in val['module']:
                    name = mod['name']
                    revision = mod['revision']
                    organization = mod['organization']
                    modules.add(name + ',' + revision + ',' + organization)
            else:
                iterate_in_depth(val, modules)


def make_cache(credentials, response, protocol, confd_ip, confd_port, api_protocol, api_port):
    try:
        try:
            os.makedirs('./cache')
        except OSError as e:
            # be happy if someone already created the path
            if e.errno != errno.EEXIST:
                return __response_type[0] + '#split#Server error - could not create directory'

        path = protocol + '://' + confd_ip + ':' + confd_port + '/api/config/catalog?deep'
        with open('./cache/catalog.json', "w") as cache_file:
            cache_file.write(http_request(path, 'GET', '', credentials, 'application/vnd.yang.data+json').read())
    except:
        e = sys.exc_info()[0]
        LOGGER.error('Could not load json to cache. Error: {}'.format(e))
        return __response_type[0] + '#split#Server error - downloading cache'
    path = api_protocol + '://' + confd_ip + ':' + api_port + '/load-cache'
    try:
        http_request(path, 'POST', '', credentials, 'application/vnd.yang.data+json').read()
    except:
        e = sys.exc_info()[0]
        LOGGER.error('Could not load json to memory-cache. Error: {}'.format(e))
        return __response_type[0] + '#split#Server error - loading to memory'
    return response


def process_module_deletion(arguments):
    credentials = arguments[3:5]
    path_to_delete = arguments[5]
    try:
        http_request(path_to_delete, 'DELETE', None, credentials, 'application/vnd.yang.data+json')
    except:
        e = sys.exc_info()[0]
        LOGGER.error('Couldn\'t delete module on path {}. Error : {}'.format(path_to_delete, e))
        return __response_type[0] + '#split#' + repr(e)
    return __response_type[1]


def on_request(ch, method, props, body):
    LOGGER.info('Received request with body {}'.format(body))
    arguments = body.split('#')

    if arguments[-3] == 'DELETE':
        if 'http' in arguments[0]:
            response = process_module_deletion(arguments)
            credentials = arguments[3:5]
            protocol, confd_ip, confd_port = arguments[0:3]
            api_protocol, api_port = arguments[-2:]
        else:
            api_protocol, api_port = arguments[-2:]
            response = process_vendor_deletion(arguments, api_protocol, api_port)
            credentials = arguments[7:9]
            protocol, confd_ip, confd_port = arguments[4:7]

    elif '--sdo' in arguments[2]:
        response = process_sdo(arguments)
        credentials = arguments[11:13]
        confd_ip = arguments[9]
        confd_port = arguments[4]
        protocol = arguments[-3]
        api_protocol, api_port = arguments[-2:]
    else:
        response = process_vendor(arguments)
        credentials = arguments[10:12]
        confd_ip = arguments[8]
        confd_port = arguments[3]
        protocol = arguments[-3]
        api_protocol, api_port = arguments[-2:]

    if response.split('#split#')[0] == __response_type[1]:
        response = make_cache(credentials, response, protocol, confd_ip, confd_port, api_protocol, api_port)

    ch.basic_publish(exchange='',
                     routing_key=props.reply_to,
                     properties=pika.BasicProperties(correlation_id=props.correlation_id),
                     body=str(response))
    ch.basic_ack(delivery_tag=method.delivery_tag)


if __name__ == '__main__':
    LOGGER.debug('Starting receiver')
    __response_type = ['Failed', 'Finished successfully']
    connection = pika.BlockingConnection(pika.ConnectionParameters(host='127.0.0.1'))
    channel = connection.channel()
    channel.queue_declare(queue='module_queue')

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(on_request, queue='module_queue')

    LOGGER.debug('Awaiting RPC request')
    channel.start_consuming()
