#!/usr/bin/env python
from datetime import datetime 
from datetime import timedelta

from lxml import etree  # Used for xml manipulation
from pprint import pformat
import argparse 
import subprocess
from subprocess import run
import json
import logging
import traceback
import logging.handlers
import os 
import pprint
import re 
import requests
import string  
import io   
import sys
import threading
import time
import yaml
import copy

from metric_collector import parser_manager
from metric_collector import netconf_collector
from metric_collector import f5_rest_collector
from metric_collector import host_manager

logging.getLogger("paramiko").setLevel(logging.INFO)
logging.getLogger("ncclient").setLevel(logging.WARNING) # In order to remove http request from ssh/paramiko
logging.getLogger("requests").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.WARNING)  # In order to remove http request from InfluxDBClient

logger = logging.getLogger("main")

### ------------------------------------------------------------------------------
### Defining the classes and procedures used later on the script
### ------------------------------------------------------------------------------

def shard_host_list(shard_id, shard_size, hosts): 
    """
    Take a dict of hosts as input and return a subset of this dict based on the size of the shard

    shard_id starts at 1
    """

    if shard_id == 0:
        return False
    elif shard_id > shard_size:
        return False

    shard_id -= 1

    hosts_list = sorted(hosts.keys())

    for i in range(0, len(hosts_list)):
        if i % shard_size != shard_id:
            del hosts[hosts_list[i]]

    return hosts


def print_format_influxdb(datapoints):
    """
    Print all datapoints to STDOUT in influxdb format for Telegraf to pick them up
    """

    for data in format_datapoints_inlineprotocol(datapoints):
        print(data)

    logger.debug('Printing Datapoint to STDOUT:')

def post_format_influxdb(datapoints, addr="http://localhost:8186/write"):
    
    s = requests.session()
    for datapoint in format_datapoints_inlineprotocol(datapoints):
        s.post(addr, data=datapoint)

    logger.info('Sending Datapoint to: %s' % addr)


def format_datapoints_inlineprotocol(datapoints):
    """
    Format all datapoints with the inlineprotocol (influxdb)
    Return a list of string formatted datapoint
    """
    
    formatted_data = []

    ## Format Tags
    if datapoints is not None:
      for datapoint in datapoints:
          tags = ''
          first_tag = 1
          for tag, value in datapoint['tags'].items():

              if first_tag == 1:
                  first_tag = 0
              else:
                  tags = tags + ','

              tags = tags + '{0}={1}'.format(tag,value)

          ## Format Measurement
          fields = ''
          first_field = 1
          for tag, value in datapoint['fields'].items():

              if first_field == 1:
                  first_field = 0
              else:
                  fields = fields + ','

              fields = fields + '{0}={1}'.format(tag,value)

          if datapoint['tags']:
            formatted_data.append("{0},{1} {2}".format(datapoint['measurement'], tags, fields))
          else:
            formatted_data.append("{0} {1}".format(datapoint['measurement'], fields))

    return formatted_data


def collector(host_list, hosts_manager, parsers_manager, 
                output_type='stdout', 
                command_tags=['.*'], 
                output_addr='http://localhost:8186/write'): 

    for host in host_list: 
        target_commands = hosts_manager.get_target_commands(host, tags=command_tags)
        credential = hosts_manager.get_credentials(host)

        host_reacheable = False

        logger.info('Collector starting for: %s', host)
        host_address = hosts_manager.get_address(host)
        device_type = hosts_manager.get_device_type(host)
        
        if device_type == 'juniper':
            dev = netconf_collector.NetconfCollector(
                    host=host, address=host_address, credential=credential, parsers=parsers_manager)
        elif device_type == 'f5':
            dev = f5_rest_collector.F5Collector(
                host=host, address=host_address, credential=credential, parsers=parsers_manager)
        dev.connect()

        if dev.is_connected():
            dev.collect_facts()
            host_reacheable = True

        else:
            logger.error('Unable to connect to %s, skipping', host)
            host_reacheable = False

        values = []
        time_execution = 0
        cmd_successful = 0
        cmd_error = 0

        if host_reacheable == True:
            time_start = time.time()
            
            ### Execute commands on the device
            for command in target_commands:
                try:
                    logger.info('[%s] Collecting > %s' % (host,command))
                    values += dev.collect(command)
                    cmd_successful += 1

                except Exception as err:
                    cmd_error += 1
                    logger.error('An issue happened while collecting %s on %s > %s ' % (host,command, err))
                    logger.error(traceback.format_exc())

            ### Save collector statistics 
            time_end = time.time()
            time_execution = time_end - time_start

        exec_time_datapoint = [{
            'measurement': 'jnpr_netconf_collector_stats',
            'tags': {
                'device': dev.hostname
            },
            'fields': {
                'execution_time_sec': "%.4f" % time_execution,
                'nbr_commands':  cmd_successful + cmd_error,
                'nbr_successful_commands':  cmd_successful,
                'nbr_error_commands':  cmd_error,
                'reacheable': int(host_reacheable),
                'unreacheable': int(not host_reacheable)
            }
        }]

        values += exec_time_datapoint

        ### if context information are provided add these in the tag list
        ### the context is a list of dict, go over all element and 
        ### check if a similar tag already exist 
        host_context = hosts_manager.get_context(host)
        for value in values:
            for item in host_context:
                for k, v in item.items():
                    if k in value['tags']: 
                        continue
                    value['tags'][k] = v

        ### Send results to the right output
        if output_type == 'stdout':
            print_format_influxdb(values)
        elif output_type == 'http':
            post_format_influxdb(values, output_addr)
        else:
            logger.warn('Output format unknown: %s', output_type)
      
      
### ------------------------------------------------------------------------------
### Create and Parse Arguments
### -----------------------------------------------------------------------------    
def main():

    time_start = time.time()

    ### ------------------------------------------------------------------------------
    ### Create and Parse Arguments
    ### -----------------------------------------------------------------------------
    # if getattr(sys, 'frozen', False):
    #     # frozen
    #     BASE_DIR = os.path.dirname(sys.executable)
    # else:
    #     # unfrozen
    #     BASE_DIR = os.path.dirname(os.path.realpath(__file__))
    
    BASE_DIR = os.getcwd()

    full_parser = argparse.ArgumentParser()
    full_parser.add_argument("--tag", nargs='+', help="Collect data from hosts that matches the tag")
    full_parser.add_argument("--cmd-tag", nargs='+', help="Collect data from command that matches the tag")
    
    full_parser.add_argument("-c", "--console", action='store_true', help="Console logs enabled")
    full_parser.add_argument( "--test", action='store_true', help="Use emulated Junos device")
    full_parser.add_argument("-s", "--start", action='store_true', help="Start collecting (default 'no')")
    full_parser.add_argument("-i", "--input", default=BASE_DIR, help="Directory where to find input files")

    full_parser.add_argument("--loglvl", default=20, help="Logs verbosity, 10-debug, 50 Critical")

    full_parser.add_argument("--logdir", default="logs", help="Directory where to store logs")
    
    full_parser.add_argument("--sharding",  help="Define if the script is part of a shard need to include the place in the shard and the size of the shard [0/3]")
    full_parser.add_argument("--sharding-offset", default=True, help="Define an offset needs to be applied to the shard_id")

    full_parser.add_argument("--parserdir", default="parsers", help="Directory where to find parsers")
    full_parser.add_argument("--timeout", default=600, help="Default Timeout for Netconf session")
    full_parser.add_argument("--delay", default=3, help="Delay Between Commands")
    full_parser.add_argument("--retry", default=5, help="Max retry")
    full_parser.add_argument("--usehostname", default=True, help="Use hostname from device instead of IP")
    full_parser.add_argument("--dbschema", default=2, help="Format of the output data")

    full_parser.add_argument("--host", default=None, help="Host DNS or IP")
    full_parser.add_argument("--hosts", default="hosts.yaml", help="Hosts file in yaml")
    full_parser.add_argument("--commands", default="commands.yaml", help="Commands file in Yaml")
    full_parser.add_argument("--credentials", default="credentials.yaml", help="Credentials file in Yaml")

    full_parser.add_argument("--output-format", default="influxdb", help="Format of the output")
    full_parser.add_argument("--output-type", default="stdout", choices=['stdout', 'http'], help="Type of output")
    full_parser.add_argument("--output-addr", default="http://localhost:8186/write", help="Addr information for output action")

    full_parser.add_argument("--use-thread", default=True, help="Spawn multiple threads to collect the information on the devices")
    full_parser.add_argument("--nbr-thread", default=10, help="Maximum number of thread to spawn (default 10)")

    dynamic_args = vars(full_parser.parse_args())

    # Print help if no parameters are provided
    if len(sys.argv)==1:
        full_parser.print_help()
        sys.exit(1)

    ## Change BASE_DIR_INPUT if we are in "test" mode
    if dynamic_args['test']:
        BASE_DIR_INPUT = dynamic_args['input']

    ### ------------------------------------------------------------------------------
    # Loading YAML Default Variables
    ### ------------------------------------------------------------------------------
    db_schema = dynamic_args['dbschema']
    max_connection_retries = dynamic_args['retry']
    delay_between_commands = dynamic_args['delay']
    logging_level = dynamic_args['loglvl']
    default_junos_rpc_timeout = dynamic_args['timeout']
    use_hostname = dynamic_args['usehostname']

    ### ------------------------------------------------------------------------------
    ### Validate Arguments
    ### ------------------------------------------------------------------------------
    pp = pprint.PrettyPrinter(indent=4)

    tag_list = []
    ###  Known and fixed arguments
    if dynamic_args['tag']:
        tag_list = dynamic_args['tag']
    else:
        tag_list = [ ".*" ]

    if not(dynamic_args['start']):
        print('Missing <start> option, so nothing to do')
        sys.exit(0)

    ### ------------------------------------------------------------------------------
    ### Logging
    ### ------------------------------------------------------------------------------
    timestamp = time.strftime("%Y-%m-%d", time.localtime(time.time()))
    log_dir = BASE_DIR + "/" + dynamic_args['logdir']
    logger = logging.getLogger("main")

    ## Check that logs directory exist, create it if needed
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    formatter = '%(asctime)s %(name)s %(levelname)s %(threadName)-10s:  %(message)s'
    logging.basicConfig(filename=log_dir + "/"+ timestamp + '_py_netconf.log',
                        level=logging_level,
                        format=formatter,
                        datefmt='%Y-%m-%d %H:%M:%S')

    if dynamic_args['console']:
        logger.info("Console logs enabled")
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG)
        logging.getLogger('').addHandler(console)

    ### ------------------------------------------------------------------------------
    ### LOAD all credentials in a dict
    ### ------------------------------------------------------------------------------
    credentials = {}
    credentials_yaml_file = ''

    if os.path.isfile(dynamic_args['credentials']):
        credentials_yaml_file = dynamic_args['credentials']
    else:
        credentials_yaml_file = BASE_DIR + "/"+ dynamic_args['credentials']

    logger.info('Importing credentials file: %s ',credentials_yaml_file)
    try:
        with open(credentials_yaml_file) as f:
            credentials = yaml.load(f)
    except Exception as e:
        logger.error('Error importing credentials file: %s', credentials_yaml_file)
        sys.exit(0)

    ### ------------------------------------------------------------------------------
    ###  LOAD all hosts     
    ###    Host list can come from a yaml file or from a dynamic inventory script
    ###    Try to load as Yaml First, than try to execute the script an import JSON
    ### ------------------------------------------------------------------------------
    
    hosts = {}

    if os.path.isfile(dynamic_args['hosts']):
        hosts_file = dynamic_args['hosts']
    else:
        hosts_file = BASE_DIR + "/"+ dynamic_args['hosts']

    logger.info('Importing host file: %s ',hosts_file)

    is_yaml = False
    is_exec = False
    try:
        with open(hosts_file) as f:
            hosts = yaml.load(f)
        is_yaml = True
    except Exception as e:
        logger.debug('Error importing host file in yaml: %s > %s' % (hosts_file, e))

    if not is_yaml:
        try:
            output_str = run(["python", hosts_file], stdout=subprocess.PIPE)
            hosts = json.loads(output_str.stdout)
            is_exec = True
        except Exception as e:
            logger.debug('Error importing executing host file: %s > %s' % (hosts_file, e))

    if not is_yaml and not is_exec:
        logger.error('Unable to import the hosts file (%s), either in Yaml or from a dynamic inventory',hosts_file)
        sys.exit(0)

    if 'sharding' in dynamic_args and dynamic_args['sharding'] != None:

        sharding_param = dynamic_args['sharding'].split('/')

        if len(sharding_param) != 2:
            logger.error('Sharding Parameters not valid %s' % dynamic_args['sharding'])
            sys.exit(0)

        shard_id = int(sharding_param[0])
        shard_size = int(sharding_param[1])

        if dynamic_args['sharding_offset']:
            shard_id += 1

        hosts = shard_host_list(shard_id, shard_size, hosts)

    ### ------------------------------------------------------------------------------
    ### LOAD all commands with their tags in a dict           
    ### ------------------------------------------------------------------------------
    commands_yaml_file = ''
    commands = []

    if os.path.isfile(dynamic_args['commands']):
        commands_yaml_file = dynamic_args['commands']
    else:
        commands_yaml_file = BASE_DIR + "/"+ dynamic_args['commands']

    logger.info('Importing commands file: %s ',commands_yaml_file)
    with open(commands_yaml_file) as f:
        try:
            for document in yaml.load_all(f):
                commands.append(document)
        except Exception as e:
            logger.error('Error importing commands file: %s', commands_yaml_file)
            sys.exit(0)

    general_commands = commands[0]

    ### ------------------------------------------------------------------------------
    ### LOAD all parsers                                      
    ### ------------------------------------------------------------------------------
    parsers_manager = parser_manager.ParserManager( parser_dirs = dynamic_args['parserdir'] )
    hosts_manager = host_manager.HostManager(
        inventory=hosts, 
        credentials=credentials,
        commands=general_commands
    )

    logger.debug('Getting hosts that matches the specified tags')
    #  Get all hosts that matches with the tags
    target_hosts = hosts_manager.get_target_hosts(tags=tag_list)
    logger.debug('The following hosts are being selected: %s', target_hosts)

    use_threads = dynamic_args['use_thread']
    
    if dynamic_args['cmd_tag']: 
        command_tags = dynamic_args['cmd_tag']
    else:
        command_tags = ['.*']

    if use_threads:
        max_collector_threads = int(dynamic_args['nbr_thread'])
        target_hosts_lists = [target_hosts[x:x+int(len(target_hosts)/max_collector_threads+1)] for x in range(0, len(target_hosts), int(len(target_hosts)/max_collector_threads+1))]

        jobs = []
        i=1
        for target_hosts_list in target_hosts_lists:
            logger.info('Collector Thread-%s scheduled with following hosts: %s', i, target_hosts_list)
            thread = threading.Thread(target=collector, 
                                      kwargs={"host_list":target_hosts_list,
                                              "parsers_manager":parsers_manager,
                                              "hosts_manager":hosts_manager,
                                              "output_type":dynamic_args['output_type'],
                                              "output_addr":dynamic_args['output_addr'],
                                              "command_tags": command_tags,
                                              })
            jobs.append(thread)
            i=i+1

        # Start the threads
        for j in jobs:
            j.start()

        # Ensure all of the threads have finished
        for j in jobs:
            j.join()
    
    else:
        # Execute everythings in the main thread
        for host in target_hosts:
            collector([host],parsers_manager=parsers_manager,
                             hosts_manager=hosts_manager)
    
    ### -----------------------------------------------------
    ### Collect Global Statistics 
    ### -----------------------------------------------------
    time_end = time.time()
    time_execution = time_end - time_start

    global_datapoint = [{
            'measurement': 'jnpr_metric_collector_stats_agent',
            'tags': {},
            'fields': {
                'execution_time_sec': "%.4f" % time_execution,
                'nbr_devices': len(target_hosts)
            }
        }]

    if 'sharding' in dynamic_args and dynamic_args['sharding'] != None:
        global_datapoint[0]['tags']['sharding'] = dynamic_args['sharding']
    
    if use_threads:
        global_datapoint[0]['fields']['nbr_threads'] = dynamic_args['nbr_thread']

    ### Send results to the right output
    if dynamic_args['output_type'] == 'stdout':
        print_format_influxdb(global_datapoint)
    elif dynamic_args['output_type'] == 'http':
        post_format_influxdb(global_datapoint, dynamic_args['output_addr'],)
    else:
        logger.warn('Output format unknown: %s', dynamic_args['output_type'])
    

if __name__ == "__main__":
    main()
