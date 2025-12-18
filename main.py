#!/usr/bin/env python3
# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""
CV Job Informer - Entry Point

Main entry point for the Kubernetes Job Monitoring Service.
Parses command-line arguments and starts the JobMonitor.
"""

import argparse
import logging
import sys

import constants
from job_monitor import JobMonitor

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s',
                    handlers=[
                        logging.StreamHandler(sys.stdout),
                        logging.FileHandler('job_monitor.log')
                    ])
logger = logging.getLogger(__name__)


def parse_namespaces(namespace_arg: str) -> set:
    """Parse namespace argument into a set of namespaces."""
    if not namespace_arg or namespace_arg.strip() == '':
        return set()  # Empty set means watch all namespaces

    # Split by comma and strip whitespace
    namespaces = {ns.strip() for ns in namespace_arg.split(',') if ns.strip()}
    return namespaces


def main():
    """Main entry point for the CV Job Informer."""
    parser = argparse.ArgumentParser(
        description=
        "Monitor Kubernetes jobs (dynamically discovers parent resources from pod ownerReferences) "
        "and send events to JobConfig API and optional NodeConfig API")
    parser.add_argument(
        '--namespace',
        default='',
        help=
        'Namespace(s) to monitor. Accepts: empty/unspecified for all namespaces, '
        'single namespace (e.g., "default"), or comma-separated list (e.g., "ns1,ns2,ns3"). '
        'Default: all namespaces')
    parser.add_argument('--log-level',
                        default='info',
                        choices=[
                            'debug', 'info', 'warning', 'error', 'DEBUG',
                            'INFO', 'WARNING', 'ERROR'
                        ],
                        help='Log level (default: info)')
    parser.add_argument(
        '--api-server',
        required=True,
        help='CloudVision API server address (e.g., www.arista.io)')
    parser.add_argument('--api-token',
                        required=True,
                        help='Authentication token for JobConfig API')
    parser.add_argument(
        '--location',
        required=True,
        help='Location identifier for API calls (e.g., cluster name)')
    parser.add_argument(
        '--jobconfig-mode',
        default='interface',
        choices=['node', 'interface'],
        help=
        'JobConfig mode: "node" for jobs with exclusive node allocation (all interfaces on nodes used), '
        '"interface" for jobs with partial interface allocation per node (only specific interface MAC addresses used). '
        'Default: interface')
    parser.add_argument(
        '--nodeconfig-mode',
        default='discovery',
        choices=['discovery', 'sriovoperator', 'disabled'],
        help=
        ('NodeConfig mode: "discovery" (use cv-interface-discovery daemonset), '
         '"sriovoperator" (use SR-IOV Network Operator), or "disabled" (no NodeConfig). '
         'Default: discovery'))
    parser.add_argument(
        '--node-interface-type',
        default='all',
        choices=['all', 'pf', 'vf'],
        help=('Which node interfaces to send to NodeConfig API: "all" '
              '(PFs + VFs), "pf" (PFs only), or "vf" (VFs only). '
              'Default: all'))
    args = parser.parse_args()

    # Set log level from argument (convert to uppercase for Python logging module)
    logging.getLogger().setLevel(getattr(logging, args.log_level.upper()))

    # Suppress verbose Kubernetes client logs
    logging.getLogger('kubernetes').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    # Set API configuration from arguments (all required)
    constants.API_SERVER = args.api_server
    constants.API_TOKEN = args.api_token
    constants.JOBCONFIG_MODE = args.jobconfig_mode
    logger.info(f"[INIT] CV API Server: {constants.API_SERVER}")
    logger.info(f"[INIT] CV JobConfig mode: {constants.JOBCONFIG_MODE}")

    nodeconfig_mode = args.nodeconfig_mode
    node_interface_type = args.node_interface_type

    # Parse namespace argument into a set
    namespaces = parse_namespaces(args.namespace)

    monitor = JobMonitor(namespaces=namespaces,
                         location=args.location,
                         nodeconfig_mode=nodeconfig_mode,
                         node_interface_type=node_interface_type)
    monitor.run()


if __name__ == "__main__":
    main()
