import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pika
import pyshark
import redis


def connect_rabbit(host='messenger', port=5672, queue='task_queue'):
    params = pika.ConnectionParameters(host=host, port=port)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.queue_declare(queue=queue, durable=True)
    return channel

def send_rabbit_msg(msg, channel, exchange='', routing_key='task_queue'):
    channel.basic_publish(exchange=exchange,
                          routing_key=routing_key,
                          body=json.dumps(msg),
                          properties=pika.BasicProperties(delivery_mode=2))
    print(" [X] %s UTC %r %r" % (str(datetime.datetime.utcnow()),
                                 str(msg['id']), str(msg['file_path'])))
    return

def get_version():
    with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'VERSION'), 'r') as f:
        return f.read().strip()

def run_proc(args, output=subprocess.DEVNULL):
    proc = subprocess.Popen(args, stdout=output)
    return proc.communicate()

def run_p0f(path):
    with tempfile.TemporaryDirectory() as tempdir:
        p0f = shutil.which('p0f')
        p0f_output = os.path.join(tempdir, 'p0f_output.txt')
        args = [p0f, '-r', path, '-o', p0f_output]
        run_proc(args)
        with open(p0f_output, 'r') as f:
            return f.read()

def parse_ip(packet):
    for ip_type in ('ip', 'ipv6'):
        try:
            ip_fields = getattr(packet, ip_type)
        except AttributeError:
            continue
        src_ip_address = getattr(ip_fields, '%s.src' % ip_type)
        dst_ip_address = getattr(ip_fields, '%s.dst' % ip_type)
        return (src_ip_address, dst_ip_address)
    return (None, None)

def parse_eth(packet):
    src_eth_address = packet.eth.src
    dst_eth_address = packet.eth.dst
    return (src_eth_address, dst_eth_address)

def run_tshark(path):
    src_addresses = set()
    with pyshark.FileCapture(path, use_json=True, include_raw=False, keep_packets=False,
                             custom_parameters=['-o', 'tcp.desegment_tcp_streams:false', '-n']) as cap:
        for packet in cap:
            src_eth_address, _ = parse_eth(packet)
            src_address, _ = parse_ip(packet)
            if src_eth_address and src_address:
                src_addresses.add((src_address, src_eth_address))
    return src_addresses

def parse_output(p0f_output, src_addresses):
    results = {}
    for line in p0f_output.splitlines():
        l = " ".join(line.split()[2:])
        l = l.split('|')
        if l[0] == 'mod=syn':
            results[l[1].split('cli=')[1].split('/')[0]] = {'full_os': l[4].split('os=')[1], 'short_os': l[4].split('os=')[1].split()[0]}
    for src_address, src_eth_address in src_addresses:
        if src_address in results:
            results[src_address]['mac'] = src_eth_address
    return results

def connect():
    r = None
    try:
        r = redis.StrictRedis(host='redis', port=6379, db=0)
    except Exception as e:  # pragma: no cover
        try:
            r = redis.StrictRedis(host='localhost', port=6379, db=0)
        except Exception as e:  # pragma: no cover
            print('Unable to connect to redis because: ' + str(e))
    return r

def save(r, results):
    timestamp = str(int(time.time()))
    if r:
        try:
            if isinstance(results, list):
                for result in results:
                    for key in result:
                        redis_k = {}
                        for k in result[key]:
                            redis_k[k] = str(result[key][k])
                        r.hmset(key, redis_k)
                        r.hmset('p0f_'+timestamp+'_'+key, redis_k)
                        r.sadd('ip_addresses', key)
                        r.sadd('p0f_timestamps', timestamp)
            elif isinstance(results, dict):
                for key in results:
                    redis_k = {}
                    for k in results[key]:
                        redis_k[k] = str(results[key][k])
                    r.hmset(key, redis_k)
                    r.hmset('p0f_'+timestamp+'_'+key, redis_k)
                    r.sadd('ip_addresses', key)
                    r.sadd('p0f_timestamps', timestamp)
        except Exception as e:  # pragma: no cover
            print('Unable to store contents of p0f: ' + str(results) +
                  ' in redis because: ' + str(e))

def ispcap(pathfile):
    for ext in ('pcap', 'pcapng', 'dump', 'capture'):
        if pathfile.endswith(''.join(('.', ext))):
            return True
    return False

def main():
    pcap_paths = []
    path = sys.argv[1]
    if os.path.isdir(path):
        for root, _, files in os.walk(path):
            for pathfile in files:
                if ispcap(pathfile):
                    pcap_paths.append(os.path.join(root, pathfile))
    else:
        pcap_paths.append(path)


    for path in pcap_paths:
        p0f_output = run_p0f(path)
        src_addresses = run_tshark(path)
        results = parse_output(p0f_output, src_addresses)
        print(results)

        if os.environ.get('redis', '') == 'true':
            r = connect()
            save(r, results)

        if os.environ.get('rabbit', '') == 'true':
            uid = os.environ.get('id', '')
            version = get_version()
            try:
                channel = connect_rabbit()
                body = {'id': uid, 'type': 'metadata', 'file_path': path, 'data': results, 'results': {'tool': 'p0f', 'version': version}}
                send_rabbit_msg(body, channel)
                if path == pcap_paths[-1]:
                    body = {'id': uid, 'type': 'metadata', 'file_path': path, 'data': '', 'results': {'tool': 'p0f', 'version': version}}
                    send_rabbit_msg(body, channel)
            except Exception as e:
                print(str(e))


if __name__ == "__main__":  # pragma: no cover
    main()
