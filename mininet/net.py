"""

    Mininet: A simple networking testbed for OpenFlow/SDN!

author: Bob Lantz (rlantz@cs.stanford.edu)
author: Brandon Heller (brandonh@stanford.edu)

Mininet creates scalable OpenFlow test networks by using
process-based virtualization and network namespaces.

Simulated hosts are created as processes in separate network
namespaces. This allows a complete OpenFlow network to be simulated on
top of a single Linux kernel.

Each host has:

A virtual console (pipes to a shell)
A virtual interfaces (half of a veth pair)
A parent shell (and possibly some child processes) in a namespace

Hosts have a network interface which is configured via ifconfig/ip
link/etc.

This version supports both the kernel and user space datapaths
from the OpenFlow reference implementation (openflowswitch.org)
as well as OpenVSwitch (openvswitch.org.)

In kernel datapath mode, the controller and switches are simply
processes in the root namespace.

Kernel OpenFlow datapaths are instantiated using dpctl(8), and are
attached to the one side of a veth pair; the other side resides in the
host namespace. In this mode, switch processes can simply connect to the
controller via the loopback interface.

In user datapath mode, the controller and switches can be full-service
nodes that live in their own network namespaces and have management
interfaces and IP addresses on a control network (e.g. 192.168.123.1,
currently routed although it could be bridged.)

In addition to a management interface, user mode switches also have
several switch interfaces, halves of veth pairs whose other halves
reside in the host nodes that the switches are connected to.

Consistent, straightforward naming is important in order to easily
identify hosts, switches and controllers, both from the CLI and
from program code. Interfaces are named to make it easy to identify
which interfaces belong to which node.

The basic naming scheme is as follows:

    Host nodes are named h1-hN
    Switch nodes are named s1-sN
    Controller nodes are named c0-cN
    Interfaces are named {nodename}-eth0 .. {nodename}-ethN

Note: If the network topology is created using mininet.topo, then
node numbers are unique among hosts and switches (e.g. we have
h1..hN and SN..SN+M) and also correspond to their default IP addresses
of 10.x.y.z/8 where x.y.z is the base-256 representation of N for
hN. This mapping allows easy determination of a node's IP
address from its name, e.g. h1 -> 10.0.0.1, h257 -> 10.0.1.1.

Note also that 10.0.0.1 can often be written as 10.1 for short, e.g.
"ping 10.1" is equivalent to "ping 10.0.0.1".

Currently we wrap the entire network in a 'mininet' object, which
constructs a simulated network based on a network topology created
using a topology object (e.g. LinearTopo) from mininet.topo or
mininet.topolib, and a Controller which the switches will connect
to. Several configuration options are provided for functions such as
automatically setting MAC addresses, populating the ARP table, or
even running a set of terminals to allow direct interaction with nodes.

After the network is created, it can be started using start(), and a
variety of useful tasks maybe performed, including basic connectivity
and bandwidth tests and running the mininet CLI.

Once the network is up and running, test code can easily get access
to host and switch objects which can then be used for arbitrary
experiments, typically involving running a series of commands on the
hosts.

After all desired tests or activities have been completed, the stop()
method may be called to shut down the network.

"""

import os
import re
import select
import signal
import random
import shlex
import ipaddress

from time import sleep
from itertools import chain, groupby
from math import ceil

from mininet.cli import CLI
from mininet.log import info, error, debug, output, warn
from mininet.node import ( Node, Docker, LibvirtHost, Host, OVSKernelSwitch,
                           DefaultController, Controller, OVSSwitch, OVSBridge )
from mininet.nodelib import NAT
from mininet.link import Link, Intf
from mininet.util import ( quietRun, fixLimits, numCores, ensureRoot,
                           macColonHex, ipStr, ipParse, netParse, ipAdd,
                           waitListening, libvirtErrorHandler )
from mininet.term import cleanUpScreens, makeTerms

from subprocess import Popen

LIBVIRT_AVAILABLE = False
try:
    import libvirt
    import xml.dom.minidom as minidom
    LIBVIRT_AVAILABLE = True
except ImportError:
    info("No libvirt functionality present. Can not deploy virtual machines.")
    info("Install libvirt-python as well as python-paramiko to enable virtual machine emulation.")

# Mininet version: should be consistent with README and LICENSE
VERSION = "2.3.0d1"

# If an external SAP (Service Access Point) is made, it is deployed with this prefix in the name,
# so it can be removed at a later time
SAP_PREFIX = 'sap.'

class Mininet( object ):
    "Network emulation with hosts spawned in network namespaces."

    def __init__( self, topo=None, switch=OVSKernelSwitch, host=Host,
                  controller=DefaultController, link=Link, intf=Intf,
                  build=True, xterms=False, cleanup=False, ipBase='10.0.0.0/8',
                  inNamespace=False,
                  autoSetMacs=False, autoStaticArp=False, autoPinCpus=False,
                  listenPort=None, waitConnected=False ):
        """Create Mininet object.
           topo: Topo (topology) object or None
           switch: default Switch class
           host: default Host class/constructor
           controller: default Controller class/constructor
           link: default Link class/constructor
           intf: default Intf class/constructor
           ipBase: base IP address for hosts,
           build: build now from topo?
           xterms: if build now, spawn xterms?
           cleanup: if build now, cleanup before creating?
           inNamespace: spawn switches and controller in net namespaces?
           autoSetMacs: set MAC addrs automatically like IP addresses?
           autoStaticArp: set all-pairs static MAC addrs?
           autoPinCpus: pin hosts to (real) cores (requires CPULimitedHost)?
           listenPort: base listening port to open; will be incremented for
               each additional switch in the net if inNamespace=False"""
        self.topo = topo
        self.switch = switch
        self.host = host
        self.controller = controller
        self.link = link
        self.intf = intf
        self.ipBase = ipBase
        self.ipBaseNum, self.prefixLen = netParse( self.ipBase )
        self.nextIP = 1  # start for address allocation
        self.inNamespace = inNamespace
        self.xterms = xterms
        self.cleanup = cleanup
        self.autoSetMacs = autoSetMacs
        self.autoStaticArp = autoStaticArp
        self.autoPinCpus = autoPinCpus
        self.numCores = numCores()
        self.nextCore = 0  # next core for pinning hosts to CPUs
        self.listenPort = listenPort
        self.waitConn = waitConnected

        self.hosts = []
        self.switches = []
        self.controllers = []
        self.links = []

        self.nameToNode = {}  # name to Node (Host/Switch) objects

        self.terms = []  # list of spawned xterm processes

        Mininet.init()  # Initialize Mininet if necessary

        self.built = False
        if topo and build:
            self.build()

    def waitConnected( self, timeout=None, delay=.5 ):
        """wait for each switch to connect to a controller,
           up to 5 seconds
           timeout: time to wait, or None to wait indefinitely
           delay: seconds to sleep per iteration
           returns: True if all switches are connected"""
        info( '*** Waiting for switches to connect\n' )
        time = 0
        remaining = list( self.switches )
        while True:
            for switch in tuple( remaining ):
                if switch.connected():
                    info( '%s ' % switch )
                    remaining.remove( switch )
            if not remaining:
                info( '\n' )
                return True
            if time > timeout and timeout is not None:
                break
            sleep( delay )
            time += delay
        warn( 'Timed out after %d seconds\n' % time )
        for switch in remaining:
            if not switch.connected():
                warn( 'Warning: %s is not connected to a controller\n'
                      % switch.name )
            else:
                remaining.remove( switch )
        return not remaining

    def getNextIp( self ):
        ip = ipAdd( self.nextIP,
                    ipBaseNum=self.ipBaseNum,
                    prefixLen=self.prefixLen ) + '/%s' % self.prefixLen
        self.nextIP += 1
        return ip

    def addHost( self, name, cls=None, **params ):
        """Add host.
           name: name of host to add
           cls: custom host class/constructor (optional)
           params: parameters for host
           returns: added host"""
        # Default IP and MAC addresses
        defaults = { 'ip': ipAdd( self.nextIP,
                                  ipBaseNum=self.ipBaseNum,
                                  prefixLen=self.prefixLen ) +
                                  '/%s' % self.prefixLen }
        if self.autoSetMacs:
            defaults[ 'mac' ] = macColonHex( self.nextIP )
        if self.autoPinCpus:
            defaults[ 'cores' ] = self.nextCore
            self.nextCore = ( self.nextCore + 1 ) % self.numCores
        self.nextIP += 1
        defaults.update( params )
        if not cls:
            cls = self.host
        h = cls( name, **defaults )
        self.hosts.append( h )
        self.nameToNode[ name ] = h
        return h

    def removeHost( self, name, **params):
        """
        Remove a host from the network at runtime.
        """
        if not isinstance( name, basestring ) and name is not None:
            name = name.name  # if we get a host object
        try:
            h = self.get(name)
        except:
            error("Host: %s not found. Cannot remove it.\n" % name)
            return False
        if h is not None:
            if h in self.hosts:
                self.hosts.remove(h)
            if name in self.nameToNode:
                del self.nameToNode[name]
            h.stop( deleteIntfs=True )
            debug("Removed: %s\n" % name)
            return True
        return False

    def addSwitch( self, name, cls=None, **params ):
        """Add switch.
           name: name of switch to add
           cls: custom switch class/constructor (optional)
           returns: added switch
           side effect: increments listenPort ivar ."""
        defaults = { 'listenPort': self.listenPort,
                     'inNamespace': self.inNamespace }
        defaults.update( params )
        if not cls:
            cls = self.switch
        sw = cls( name, **defaults )
        if not self.inNamespace and self.listenPort:
            self.listenPort += 1
        self.switches.append( sw )
        self.nameToNode[ name ] = sw
        return sw

    def addController( self, name='c0', controller=None, **params ):
        """Add controller.
           controller: Controller class"""
        # Get controller class
        if not controller:
            controller = self.controller
        # Construct new controller if one is not given
        if isinstance( name, Controller ):
            controller_new = name
            # Pylint thinks controller is a str()
            # pylint: disable=maybe-no-member
            name = controller_new.name
            # pylint: enable=maybe-no-member
        else:
            controller_new = controller( name, **params )
        # Add new controller to net
        if controller_new:  # allow controller-less setups
            self.controllers.append( controller_new )
            self.nameToNode[ name ] = controller_new
        return controller_new

    def addNAT( self, name='nat0', connect=True, inNamespace=False,
                **params):
        """Add a NAT to the Mininet network
           name: name of NAT node
           connect: switch to connect to | True (s1) | None
           inNamespace: create in a network namespace
           params: other NAT node params, notably:
               ip: used as default gateway address"""
        nat = self.addHost( name, cls=NAT, inNamespace=inNamespace,
                            subnet=self.ipBase, **params )
        # find first switch and create link
        if connect:
            if not isinstance( connect, Node ):
                # Use first switch if not specified
                connect = self.switches[ 0 ]
            # Connect the nat to the switch
            self.addLink( nat, connect )
            # Set the default route on hosts
            natIP = nat.params[ 'ip' ].split('/')[ 0 ]
            for host in self.hosts:
                if host.inNamespace:
                    host.setDefaultRoute( 'via %s' % natIP )
        return nat

    # BL: We now have four ways to look up nodes
    # This may (should?) be cleaned up in the future.
    def getNodeByName( self, *args ):
        "Return node(s) with given name(s)"
        if len( args ) == 1:
            return self.nameToNode.get(args[0])
        return [ self.nameToNode.get(n) for n in args ]

    def get( self, *args ):
        "Convenience alias for getNodeByName"
        return self.getNodeByName( *args )

    # Even more convenient syntax for node lookup and iteration
    def __getitem__( self, key ):
        """net [ name ] operator: Return node(s) with given name(s)"""
        return self.nameToNode[ key ]

    def __iter__( self ):
        "return iterator over node names"
        for node in chain( self.hosts, self.switches, self.controllers ):
            yield node.name

    def __len__( self ):
        "returns number of nodes in net"
        return ( len( self.hosts ) + len( self.switches ) +
                 len( self.controllers ) )

    def __contains__( self, item ):
        "returns True if net contains named node"
        return item in self.nameToNode

    def keys( self ):
        "return a list of all node names or net's keys"
        return list( self )

    def values( self ):
        "return a list of all nodes or net's values"
        return [ self[name] for name in self ]

    def items( self ):
        "return (key,value) tuple list for every node in net"
        return zip( self.keys(), self.values() )

    @staticmethod
    def randMac():
        "Return a random, non-multicast MAC address"
        return macColonHex( random.randint(1, 2**48 - 1) & 0xfeffffffffff |
                            0x020000000000 )

    def addLink( self, node1, node2, port1=None, port2=None,
                 cls=None, **params ):
        """"Add a link from node1 to node2
            node1: source node (or name)
            node2: dest node (or name)
            port1: source port (optional)
            port2: dest port (optional)
            cls: link class (optional)
            params: additional link params (optional)
            returns: link object"""
        # Accept node objects or names
        node1 = node1 if not isinstance( node1, basestring ) else self[ node1 ]
        node2 = node2 if not isinstance( node2, basestring ) else self[ node2 ]
        options = dict( params )
        # Port is optional
        if port1 is not None:
            options.setdefault( 'port1', port1 )
        if port2 is not None:
            options.setdefault( 'port2', port2 )
        if self.intf is not None:
            options.setdefault( 'intf', self.intf )
        # Set default MAC - this should probably be in Link
        options.setdefault( 'addr1', self.randMac() )
        options.setdefault( 'addr2', self.randMac() )
        cls = self.link if cls is None else cls
        link = cls( node1, node2, **options )

        # Allow to add links at runtime
        # (needs attach method provided by OVSSwitch)
        if isinstance( node1, OVSSwitch ):
            node1.attach(link.intf1)
        if isinstance( node2, OVSSwitch ):
            node2.attach(link.intf2)

        self.links.append( link )
        return link

    def removeLink(self, link=None, node1=None, node2=None):
        """
        Removes a link. Can either be specified by link object,
        or the nodes the link connects.
        """
        if link is None:
            if (isinstance( node1, basestring )
                    and isinstance( node2, basestring )):
                try:
                    node1 = self.get(node1)
                except:
                    error("Host: %s not found.\n" % node1)
                try:
                    node2 = self.get(node2)
                except:
                    error("Host: %s not found.\n" % node2)
            # try to find link by nodes
            for l in self.links:
                if l.intf1.node == node1 and l.intf2.node == node2:
                    link = l
                    break
                if l.intf1.node == node2 and l.intf2.node == node1:
                    link = l
                    break
        if link is None:
            error("Couldn't find link to be removed.\n")
            return
        # tear down the link
        link.delete()
        self.links.remove(link)

    def configHosts( self ):
        "Configure a set of hosts."
        for host in self.hosts:
            info( host.name + ' ' )
            intf = host.defaultIntf()
            if intf:
                host.configDefault()
            else:
                # Don't configure nonexistent intf
                host.configDefault( ip=None, mac=None )
            # You're low priority, dude!
            # BL: do we want to do this here or not?
            # May not make sense if we have CPU lmiting...
            # quietRun( 'renice +18 -p ' + repr( host.pid ) )
            # This may not be the right place to do this, but
            # it needs to be done somewhere.
        info( '\n' )

    def buildFromTopo( self, topo=None ):
        """Build mininet from a topology object
           At the end of this function, everything should be connected
           and up."""

        # Possibly we should clean up here and/or validate
        # the topo
        if self.cleanup:
            pass

        info( '*** Creating network\n' )

        if not self.controllers and self.controller:
            # Add a default controller
            info( '*** Adding controller\n' )
            classes = self.controller
            if not isinstance( classes, list ):
                classes = [ classes ]
            for i, cls in enumerate( classes ):
                # Allow Controller objects because nobody understands partial()
                if isinstance( cls, Controller ):
                    self.addController( cls )
                else:
                    self.addController( 'c%d' % i, cls )

        info( '*** Adding hosts:\n' )
        for hostName in topo.hosts():
            self.addHost( hostName, **topo.nodeInfo( hostName ) )
            info( hostName + ' ' )

        info( '\n*** Adding switches:\n' )
        for switchName in topo.switches():
            # A bit ugly: add batch parameter if appropriate
            params = topo.nodeInfo( switchName)
            cls = params.get( 'cls', self.switch )
            #if hasattr( cls, 'batchStartup' ):
            #    params.setdefault( 'batch', True )
            self.addSwitch( switchName, **params )
            info( switchName + ' ' )

        info( '\n*** Adding links:\n' )
        for srcName, dstName, params in topo.links(
                sort=True, withInfo=True ):
            self.addLink( **params )
            info( '(%s, %s) ' % ( srcName, dstName ) )

        info( '\n' )

    def configureControlNetwork( self ):
        "Control net config hook: override in subclass"
        raise Exception( 'configureControlNetwork: '
                         'should be overriden in subclass', self )

    def build( self ):
        "Build mininet."
        if self.topo:
            self.buildFromTopo( self.topo )
        if self.inNamespace:
            self.configureControlNetwork()
        info( '*** Configuring hosts\n' )
        self.configHosts()
        if self.xterms:
            self.startTerms()
        if self.autoStaticArp:
            self.staticArp()
        self.built = True

    def startTerms( self ):
        "Start a terminal for each node."
        if 'DISPLAY' not in os.environ:
            error( "Error starting terms: Cannot connect to display\n" )
            return
        info( "*** Running terms on %s\n" % os.environ[ 'DISPLAY' ] )
        cleanUpScreens()
        self.terms += makeTerms( self.controllers, 'controller' )
        self.terms += makeTerms( self.switches, 'switch' )
        self.terms += makeTerms( self.hosts, 'host' )

    def stopXterms( self ):
        "Kill each xterm."
        for term in self.terms:
            os.kill( term.pid, signal.SIGKILL )
        cleanUpScreens()

    def staticArp( self ):
        "Add all-pairs ARP entries to remove the need to handle broadcast."
        for src in self.hosts:
            for dst in self.hosts:
                if src != dst:
                    src.setARP( ip=dst.IP(), mac=dst.MAC() )

    def start( self ):
        "Start controller and switches."
        if not self.built:
            self.build()
        info( '*** Starting controller\n' )
        for controller in self.controllers:
            info( controller.name + ' ')
            controller.start()
        info( '\n' )
        info( '*** Starting %s switches\n' % len( self.switches ) )
        for switch in self.switches:
            info( switch.name + ' ')
            switch.start( self.controllers )
        started = {}
        for swclass, switches in groupby(
                sorted( self.switches, key=type ), type ):
            switches = tuple( switches )
            if hasattr( swclass, 'batchStartup' ):
                success = swclass.batchStartup( switches )
                started.update( { s: s for s in success } )
        info( '\n' )
        if self.waitConn:
            self.waitConnected()

    def stop( self ):
        "Stop the controller(s), switches and hosts"
        info( '*** Stopping %i controllers\n' % len( self.controllers ) )
        for controller in self.controllers:
            info( controller.name + ' ' )
            controller.stop()
        info( '\n' )
        if self.terms:
            info( '*** Stopping %i terms\n' % len( self.terms ) )
            self.stopXterms()
        info( '*** Stopping %i links\n' % len( self.links ) )
        for link in self.links:
            info( '.' )
            link.stop()
        info( '\n' )
        info( '*** Stopping %i switches\n' % len( self.switches ) )
        stopped = {}
        for swclass, switches in groupby(
                sorted( self.switches, key=type ), type ):
            switches = tuple( switches )
            if hasattr( swclass, 'batchShutdown' ):
                success = swclass.batchShutdown( switches )
                stopped.update( { s: s for s in success } )
        for switch in self.switches:
            info( switch.name + ' ' )
            if switch not in stopped:
                switch.stop()
            switch.terminate()
        info( '\n' )
        info( '*** Stopping %i hosts\n' % len( self.hosts ) )
        for host in self.hosts:
            info( host.name + ' ' )
            host.terminate()
        info( '\n*** Done\n' )

    def run( self, test, *args, **kwargs ):
        "Perform a complete start/test/stop cycle."
        self.start()
        info( '*** Running test\n' )
        result = test( *args, **kwargs )
        self.stop()
        return result

    def monitor( self, hosts=None, timeoutms=-1 ):
        """Monitor a set of hosts (or all hosts by default),
           and return their output, a line at a time.
           hosts: (optional) set of hosts to monitor
           timeoutms: (optional) timeout value in ms
           returns: iterator which returns host, line"""
        if hosts is None:
            hosts = self.hosts
        poller = select.poll()
        h1 = hosts[ 0 ]  # so we can call class method fdToNode
        for host in hosts:
            poller.register( host.stdout )
        while True:
            ready = poller.poll( timeoutms )
            for fd, event in ready:
                host = h1.fdToNode( fd )
                if event & select.POLLIN:
                    line = host.readline()
                    if line is not None:
                        yield host, line
            # Return if non-blocking
            if not ready and timeoutms >= 0:
                yield None, None

    # XXX These test methods should be moved out of this class.
    # Probably we should create a tests.py for them

    @staticmethod
    def _parsePing( pingOutput ):
        "Parse ping output and return packets sent, received."
        # Check for downed link
        if 'connect: Network is unreachable' in pingOutput:
            return 1, 0
        r = r'(\d+) packets transmitted, (\d+)( packets)? received'
        m = re.search( r, pingOutput )
        if m is None:
            error( '*** Error: could not parse ping output: %s\n' %
                   pingOutput )
            return 1, 0
        sent, received = int( m.group( 1 ) ), int( m.group( 2 ) )
        return sent, received

    def ping( self, hosts=None, timeout=None, manualdestip=None ):
        """Ping between all specified hosts.
           hosts: list of hosts
           timeout: time to wait for a response, as string
           manualdestip: sends pings from each h in hosts to manualdestip
           returns: ploss packet loss percentage"""
        # should we check if running?
        packets = 0
        lost = 0
        ploss = None
        if not hosts:
            hosts = self.hosts
            output( '*** Ping: testing ping reachability\n' )
        for node in hosts:
            output( '%s -> ' % node.name )
            if manualdestip is not None:
                opts = ''
                if timeout:
                    opts = '-W %s' % timeout
                result = node.cmd( 'ping -c1 %s %s' %
                                   (opts, manualdestip) )
                sent, received = self._parsePing( result )
                packets += sent
                if received > sent:
                    error( '*** Error: received too many packets' )
                    error( '%s' % result )
                    node.cmdPrint( 'route' )
                    exit( 1 )
                lost += sent - received
                output( ( '%s ' % manualdestip ) if received else 'X ' )
            else:
                for dest in hosts:
                    if node != dest:
                        opts = ''
                        if timeout:
                            opts = '-W %s' % timeout
                        if dest.intfs:
                            result = node.cmd( 'ping -c1 %s %s' %
                                               (opts, dest.IP()) )
                            sent, received = self._parsePing( result )
                        else:
                            sent, received = 0, 0
                        packets += sent
                        if received > sent:
                            error( '*** Error: received too many packets' )
                            error( '%s' % result )
                            node.cmdPrint( 'route' )
                            exit( 1 )
                        lost += sent - received
                        output( ( '%s ' % dest.name ) if received else 'X ' )
            output( '\n' )
        if packets > 0:
            ploss = 100.0 * lost / packets
            received = packets - lost
            output( "*** Results: %i%% dropped (%d/%d received)\n" %
                    ( ploss, received, packets ) )
        else:
            ploss = 0
            output( "*** Warning: No packets sent\n" )
        return ploss

    @staticmethod
    def _parsePingFull( pingOutput ):
        "Parse ping output and return all data."
        errorTuple = (1, 0, 0, 0, 0, 0)
        # Check for downed link
        r = r'[uU]nreachable'
        m = re.search( r, pingOutput )
        if m is not None:
            return errorTuple
        r = r'(\d+) packets transmitted, (\d+) received'
        m = re.search( r, pingOutput )
        if m is None:
            error( '*** Error: could not parse ping output: %s\n' %
                   pingOutput )
            return errorTuple
        sent, received = int( m.group( 1 ) ), int( m.group( 2 ) )
        r = r'rtt min/avg/max/mdev = '
        r += r'(\d+\.\d+)/(\d+\.\d+)/(\d+\.\d+)/(\d+\.\d+) ms'
        m = re.search( r, pingOutput )
        if m is None:
            if received == 0:
                return errorTuple
            error( '*** Error: could not parse ping output: %s\n' %
                   pingOutput )
            return errorTuple
        rttmin = float( m.group( 1 ) )
        rttavg = float( m.group( 2 ) )
        rttmax = float( m.group( 3 ) )
        rttdev = float( m.group( 4 ) )
        return sent, received, rttmin, rttavg, rttmax, rttdev

    def pingFull( self, hosts=None, timeout=None, manualdestip=None ):
        """Ping between all specified hosts and return all data.
           hosts: list of hosts
           timeout: time to wait for a response, as string
           returns: all ping data; see function body."""
        # should we check if running?
        # Each value is a tuple: (src, dsd, [all ping outputs])
        all_outputs = []
        if not hosts:
            hosts = self.hosts
            output( '*** Ping: testing ping reachability\n' )
        for node in hosts:
            output( '%s -> ' % node.name )
            if manualdestip is not None:
                opts = ''
                if timeout:
                    opts = '-W %s' % timeout
                result = node.cmd( 'ping -c1 %s %s' % (opts, manualdestip) )
                outputs = self._parsePingFull( result )
                sent, received, rttmin, rttavg, rttmax, rttdev = outputs
                all_outputs.append( (node, manualdestip, outputs) )
                output( ( '%s ' % manualdestip ) if received else 'X ' )
                output( '\n' )
            else:
                for dest in hosts:
                    if node != dest:
                        opts = ''
                        if timeout:
                            opts = '-W %s' % timeout
                        result = node.cmd( 'ping -c1 %s %s' % (opts, dest.IP()) )
                        outputs = self._parsePingFull( result )
                        sent, received, rttmin, rttavg, rttmax, rttdev = outputs
                        all_outputs.append( (node, dest, outputs) )
                        output( ( '%s ' % dest.name ) if received else 'X ' )
                output( '\n' )
        output( "*** Results: \n" )
        for outputs in all_outputs:
            src, dest, ping_outputs = outputs
            sent, received, rttmin, rttavg, rttmax, rttdev = ping_outputs
            output( " %s->%s: %s/%s, " % (src, dest, sent, received ) )
            output( "rtt min/avg/max/mdev %0.3f/%0.3f/%0.3f/%0.3f ms\n" %
                    (rttmin, rttavg, rttmax, rttdev) )
        return all_outputs

    def pingAll( self, timeout=None ):
        """Ping between all hosts.
           returns: ploss packet loss percentage"""
        return self.ping( timeout=timeout )

    def pingPair( self ):
        """Ping between first two hosts, useful for testing.
           returns: ploss packet loss percentage"""
        hosts = [ self.hosts[ 0 ], self.hosts[ 1 ] ]
        return self.ping( hosts=hosts )

    def pingAllFull( self ):
        """Ping between all hosts.
           returns: ploss packet loss percentage"""
        return self.pingFull()

    def pingPairFull( self ):
        """Ping between first two hosts, useful for testing.
           returns: ploss packet loss percentage"""
        hosts = [ self.hosts[ 0 ], self.hosts[ 1 ] ]
        return self.pingFull( hosts=hosts )

    @staticmethod
    def _parseIperf( iperfOutput ):
        """Parse iperf output and return bandwidth.
           iperfOutput: string
           returns: result string"""
        r = r'([\d\.]+ \w+/sec)'
        m = re.findall( r, iperfOutput )
        if m:
            return m[-1]
        else:
            # was: raise Exception(...)
            error( 'could not parse iperf output: ' + iperfOutput )
            return ''

    # XXX This should be cleaned up

    def iperf( self, hosts=None, l4Type='TCP', udpBw='10M', fmt=None,
               seconds=5, port=5001):
        """Run iperf between two hosts.
           hosts: list of hosts; if None, uses first and last hosts
           l4Type: string, one of [ TCP, UDP ]
           udpBw: bandwidth target for UDP test
           fmt: iperf format argument if any
           seconds: iperf time to transmit
           port: iperf port
           returns: two-element array of [ server, client ] speeds
           note: send() is buffered, so client rate can be much higher than
           the actual transmission rate; on an unloaded system, server
           rate should be much closer to the actual receive rate"""
        hosts = hosts or [ self.hosts[ 0 ], self.hosts[ -1 ] ]
        assert len( hosts ) == 2
        client, server = hosts
        output( '*** Iperf: testing', l4Type, 'bandwidth between',
                client, 'and', server, '\n' )
        server.cmd( 'killall -9 iperf' )
        iperfArgs = 'iperf -p %d ' % port
        bwArgs = ''
        if l4Type == 'UDP':
            iperfArgs += '-u '
            bwArgs = '-b ' + udpBw + ' '
        elif l4Type != 'TCP':
            raise Exception( 'Unexpected l4 type: %s' % l4Type )
        if fmt:
            iperfArgs += '-f %s ' % fmt
        server.sendCmd( iperfArgs + '-s' )
        if l4Type == 'TCP':
            if not waitListening( client, server.IP(), port ):
                raise Exception( 'Could not connect to iperf on port %d'
                                 % port )
        cliout = client.cmd( iperfArgs + '-t %d -c ' % seconds +
                             server.IP() + ' ' + bwArgs )
        debug( 'Client output: %s\n' % cliout )
        servout = ''
        # We want the last *b/sec from the iperf server output
        # for TCP, there are two fo them because of waitListening
        count = 2 if l4Type == 'TCP' else 1
        while len( re.findall( '/sec', servout ) ) < count:
            servout += server.monitor( timeoutms=5000 )
        server.sendInt()
        servout += server.waitOutput()
        debug( 'Server output: %s\n' % servout )
        result = [ self._parseIperf( servout ), self._parseIperf( cliout ) ]
        if l4Type == 'UDP':
            result.insert( 0, udpBw )
        output( '*** Results: %s\n' % result )
        return result

    def runCpuLimitTest( self, cpu, duration=5 ):
        """run CPU limit test with 'while true' processes.
        cpu: desired CPU fraction of each host
        duration: test duration in seconds (integer)
        returns a single list of measured CPU fractions as floats.
        """
        cores = int( quietRun( 'nproc' ) )
        pct = cpu * 100
        info( '*** Testing CPU %.0f%% bandwidth limit\n' % pct )
        hosts = self.hosts
        cores = int( quietRun( 'nproc' ) )
        # number of processes to run a while loop on per host
        num_procs = int( ceil( cores * cpu ) )
        pids = {}
        for h in hosts:
            pids[ h ] = []
            for _core in range( num_procs ):
                h.cmd( 'while true; do a=1; done &' )
                pids[ h ].append( h.cmd( 'echo $!' ).strip() )
        outputs = {}
        time = {}
        # get the initial cpu time for each host
        for host in hosts:
            outputs[ host ] = []
            with open( '/sys/fs/cgroup/cpuacct/%s/cpuacct.usage' %
                       host, 'r' ) as f:
                time[ host ] = float( f.read() )
        for _ in range( duration ):
            sleep( 1 )
            for host in hosts:
                with open( '/sys/fs/cgroup/cpuacct/%s/cpuacct.usage' %
                           host, 'r' ) as f:
                    readTime = float( f.read() )
                outputs[ host ].append( ( ( readTime - time[ host ] )
                                        / 1000000000 ) / cores * 100 )
                time[ host ] = readTime
        for h, pids in pids.items():
            for pid in pids:
                h.cmd( 'kill -9 %s' % pid )
        cpu_fractions = []
        for _host, outputs in outputs.items():
            for pct in outputs:
                cpu_fractions.append( pct )
        output( '*** Results: %s\n' % cpu_fractions )
        return cpu_fractions

    # BL: I think this can be rewritten now that we have
    # a real link class.
    def configLinkStatus( self, src, dst, status ):
        """Change status of src <-> dst links.
           src: node name
           dst: node name
           status: string {up, down}"""
        if src not in self.nameToNode:
            error( 'src not in network: %s\n' % src )
        elif dst not in self.nameToNode:
            error( 'dst not in network: %s\n' % dst )
        else:
            if isinstance( src, basestring ):
                src = self.nameToNode[ src ]
            if isinstance( dst, basestring ):
                dst = self.nameToNode[ dst ]
            connections = src.connectionsTo( dst )
            if len( connections ) == 0:
                error( 'src and dst not connected: %s %s\n' % ( src, dst) )
            for srcIntf, dstIntf in connections:
                result = srcIntf.ifconfig( status )
                if result:
                    error( 'link src status change failed: %s\n' % result )
                result = dstIntf.ifconfig( status )
                if result:
                    error( 'link dst status change failed: %s\n' % result )

    def interact( self ):
        "Start network and run our simple CLI."
        self.start()
        result = CLI( self )
        self.stop()
        return result

    inited = False

    @classmethod
    def init( cls ):
        "Initialize Mininet"
        if cls.inited:
            return
        ensureRoot()
        fixLimits()
        cls.inited = True


class Containernet( Mininet ):
    """
    A Mininet with Docker and Libvirt related methods.
    Inherits Mininet.
    """

    def __init__(self, **params):
        self.MANAGEMENT_NETWORK_XML = """
            <network>
                <name>{name}</name>
                <mac address="{mac}"/>
                <ip address="{ip}" netmask="{netmask}">
                    <dhcp/>
                </ip>
                <domain name="mininet.local" localOnly="yes"/>
            </network>
        """

        self.HOST_XML = """
            <host ip="{ip}" mac="{mac}" name="{name}"/>
        """
        self.libvirtManagementNetwork = None
        self.cmd_endpoint = params.pop("cmd_endpoint", "qemu:///system")
        self.lv_conn = None
        self.mgmt_dict = params.pop('mgmt_net', dict())
        self.mgmt_dict.setdefault("name", "mn.libvirt.mgmt")
        self.mgmt_dict.setdefault("ip", "192.168.134.1")
        self.mgmt_dict.setdefault("netmask", "255.255.255.0")
        self.mgmt_dict.setdefault("mac", macColonHex(ipParse(self.mgmt_dict['ip'])))
        # keep track of hosts we added to the mgmt net, technically not needed but maybe interesting for the API?
        self.leases = {}
        self.custom_mgmt_net = (self.mgmt_dict['name'] != "mn.libvirt.mgmt")
        self.libvirt = LIBVIRT_AVAILABLE
        if self.libvirt:
            libvirt.registerErrorHandler(f=libvirtErrorHandler, ctx=None)

        # call original Mininet.__init__
        Mininet.__init__(self, **params)
        self.SAPswitches = dict()


    def addDocker( self, name, cls=Docker, **params ):
        """
        Wrapper for addHost method that adds a
        Docker container as a host.
        """
        return self.addHost( name, cls=cls, **params)

    def removeDocker( self, name, **params):
        """
        Wrapper for removeHost. Just to be complete.
        """
        return self.removeHost(name, **params)

    def addHost(self, name, cls=None, **params):
        if cls is LibvirtHost:
            return self.addLibvirthost(name, cls=cls, **params)
        else:
            return Mininet.addHost(self, name, cls=cls, **params)

    def removeHost(self, name, **params):
        if isinstance(self.nameToNode[name], LibvirtHost):
            for ip, n in self.leases.items():
                if name == n:
                    del self.leases[ip]
                    break
        return self.removeHost(name, **params)

    def addLibvirthost( self, name, cls=LibvirtHost, **params ):
        """
        Wrapper for addHost method that adds a
        Libvirt-based host.
        """
        if not self.libvirt:
            error("Containernet.addLibvirthost: "
                  "No libvirt functionality available. Please install the required modules!\n")
            return False
        if self.libvirtManagementNetwork is None:
            self.lv_conn = libvirt.open(self.cmd_endpoint)
            # if a network with the given name exists use it
            # and get its configuration
            if self.custom_mgmt_net:
                try:
                    self.libvirtManagementNetwork = self.lv_conn.networkLookupByName(self.mgmt_dict['name'])
                    net_dom = minidom.parseString(self.libvirtManagementNetwork.XMLDesc())
                    for attr in net_dom.getElementsByTagName("ip")[0].attributes.items():
                        if attr[0] == "netmask":
                            self.mgmt_dict['netmask'] = attr[1]
                        if attr[0] == "address":
                            self.mgmt_dict['ip'] = attr[1]
                    for attr in net_dom.getElementsByTagName("mac")[0].attributes.items():
                        if attr[0] == "address":
                            self.mgmt_dict['mac'] = attr[1]
                    if not self.libvirtManagementNetwork.isActive():
                        self.libvirtManagementNetwork.create()
                except Exception as e:
                    error("Containernet.addLibvirthost: "
                          "Could not add the custom management network %s. %s\n" % (self.mgmt_dict['name'], e))
            else:
                self.createManagementNetwork()

        # inject the name of the management network into the host params so we can access it there
        # we use setdefault so we don't overwrite existing values in here
        params.setdefault("mgmt_network", self.mgmt_dict['name'])

        # mac address generator in mininet is flawed and will pick reserved macs!
        mac = "02:00:00:%02x:%02x:%02x" % (random.randint(0, 255),
                                     random.randint(0, 255),
                                     random.randint(0, 255))
        params.setdefault("mgmt_mac", mac)

        # find free ip for DHCP
        host_ip_int = ipParse(self.mgmt_dict['ip']) + 1
        while ipStr(host_ip_int) in self.leases:
            host_ip_int += 1

        params.setdefault("mgmt_ip", ipStr(host_ip_int))

        if params['mgmt_ip'] in self.leases:
            error("Containernet.addLibvirthost: There is already a host with IP %s. Adding host %s failed.\n" %
                  (params['mgmt_ip'], name))
            return

        if params['mgmt_ip'] != ipStr(host_ip_int):
            # statically defined node, no dhcp needed
            debug("Containernet.addLibvirthost: Host defined with static IP %s.\n" % params['mgmt_ip'])
        else:
            try:
                host_xml = self.HOST_XML.format(ip=params['mgmt_ip'], mac=params['mgmt_mac'], name=name)
                info("Containernet.addLibvirthost: Adding DHCP entry for host %s.\n" % name)
                debug("network XML:\n %s\n" % host_xml)
                #COMMANDDICT = {"none": 0, "modify": 1, "delete": 2, "add-first": 4}
                #SECTIONDICT = {"none": 0, "bridge": 1, "domain": 2, "ip": 3, "ip-dhcp-host": 4,
                #   "ip-dhcp-range": 5, "forward": 6, "forward-interface": 7,
                #   "forward-pf": 8, "portgroup": 9, "dns-host": 10, "dns-txt": 11,
                #   "dns-srv": 12}
                #FLAGSDICT = {"current": 0, "live": 1, "config": 2}
                # command, section, flags
                self.libvirtManagementNetwork.update(4, 4, 0, host_xml)
            except libvirt.libvirtError as e:
                error("Containernet.addLibvirthost: Could not update the management network. Adding host %s failed."
                      " Error: %s\n" % (name, e))
                return

        # bookkeeping
        self.leases[params['mgmt_ip']] = name
        return Mininet.addHost(self, name, cls=cls, **params)


    def removeLibvirthost( self, name, **params):
        """
        Wrapper for removeHost.
        """
        return self.removeHost(name, **params)


    def addExtSAP(self, sapName, sapIP, dpid=None, **params):
        """
        Add an external Service Access Point, implemented as an OVSBridge
        :param sapName:
        :param sapIP: str format: x.x.x.x/x
        :param dpid:
        :param params:
        :return:
        """
        SAPswitch = self.addSwitch(sapName, cls=OVSBridge, prefix=SAP_PREFIX,
                       dpid=dpid, ip=sapIP, **params)
        self.SAPswitches[sapName] = SAPswitch

        NAT = params.get('NAT', False)
        if NAT:
            self.addSAPNAT(SAPswitch)

        return SAPswitch

    def removeExtSAP(self, sapName):
        SAPswitch = self.SAPswitches[sapName]
        info( 'stopping external SAP:' + SAPswitch.name + ' \n' )
        SAPswitch.stop()
        SAPswitch.terminate()

        self.removeSAPNAT(SAPswitch)


    def addSAPNAT(self, SAPSwitch):
        """
        Add NAT to the Containernet, so external SAPs can reach the outside internet through the host
        :param SAPSwitch: Instance of the external SAP switch
        :param SAPNet: Subnet of the external SAP as str (eg. '10.10.1.0/30')
        :return:
        """
        SAPip = SAPSwitch.ip
        SAPNet = str(ipaddress.IPv4Network(unicode(SAPip), strict=False))
        # due to a bug with python-iptables, removing and finding rules does not succeed when the mininet CLI is running
        # so we use the iptables tool
        # create NAT rule
        rule0_ = "iptables -t nat -A POSTROUTING ! -o {0} -s {1} -j MASQUERADE".format(SAPSwitch.deployed_name, SAPNet)
        p = Popen(shlex.split(rule0_))
        p.communicate()

        # create FORWARD rule
        rule1_ = "iptables -A FORWARD -o {0} -j ACCEPT".format(SAPSwitch.deployed_name)
        p = Popen(shlex.split(rule1_))
        p.communicate()

        rule2_ = "iptables -A FORWARD -i {0} -j ACCEPT".format(SAPSwitch.deployed_name)
        p = Popen(shlex.split(rule2_))
        p.communicate()

        info("added SAP NAT rules for: {0} - {1}\n".format(SAPSwitch.name, SAPNet))


    def removeSAPNAT(self, SAPSwitch):

        SAPip = SAPSwitch.ip
        SAPNet = str(ipaddress.IPv4Network(unicode(SAPip), strict=False))
        # due to a bug with python-iptables, removing and finding rules does not succeed when the mininet CLI is running
        # so we use the iptables tool
        rule0_ = "iptables -t nat -D POSTROUTING ! -o {0} -s {1} -j MASQUERADE".format(SAPSwitch.deployed_name, SAPNet)
        p = Popen(shlex.split(rule0_))
        p.communicate()

        rule1_ = "iptables -D FORWARD -o {0} -j ACCEPT".format(SAPSwitch.deployed_name)
        p = Popen(shlex.split(rule1_))
        p.communicate()

        rule2_ = "iptables -D FORWARD -i {0} -j ACCEPT".format(SAPSwitch.deployed_name)
        p = Popen(shlex.split(rule2_))
        p.communicate()

        info("remove SAP NAT rules for: {0} - {1}\n".format(SAPSwitch.name, SAPNet))

    def createManagementNetwork(self):
        # build XML
        network_xml = self.MANAGEMENT_NETWORK_XML.format(
            name=self.mgmt_dict['name'],
            mac=self.mgmt_dict['mac'],
            ip=self.mgmt_dict['ip'],
            netmask=self.mgmt_dict['netmask']
        )

        debug("Containernet.createManagementNetwork: Instantiating default management network for libvirt:\n%s\n" %
              network_xml)
        self.libvirtManagementNetwork = self.lv_conn.networkCreateXML(network_xml)

    def destroyManagementNetwork(self):
        if self.libvirtManagementNetwork is not None and not self.custom_mgmt_net:
            debug('*** Removing the libvirt management network ***\n')
            self.libvirtManagementNetwork.destroy()
            self.libvirtManagementNetwork = None
            self.lv_conn = None
            self.allocated_dhcp_ips = 1
            self.leases = []

    def stop(self):
        super(Containernet, self).stop()

        info('*** Removing NAT rules of %i SAPs\n' % len(self.SAPswitches))
        for SAPswitch in self.SAPswitches:
            self.removeSAPNAT(self.SAPswitches[SAPswitch])
        info("\n")
        self.destroyManagementNetwork()


class MininetWithControlNet( Mininet ):

    """Control network support:

       Create an explicit control network. Currently this is only
       used/usable with the user datapath.

       Notes:

       1. If the controller and switches are in the same (e.g. root)
          namespace, they can just use the loopback connection.

       2. If we can get unix domain sockets to work, we can use them
          instead of an explicit control network.

       3. Instead of routing, we could bridge or use 'in-band' control.

       4. Even if we dispense with this in general, it could still be
          useful for people who wish to simulate a separate control
          network (since real networks may need one!)

       5. Basically nobody ever used this code, so it has been moved
          into its own class.

       6. Ultimately we may wish to extend this to allow us to create a
          control network which every node's control interface is
          attached to."""

    def configureControlNetwork( self ):
        "Configure control network."
        self.configureRoutedControlNetwork()

    # We still need to figure out the right way to pass
    # in the control network location.

    def configureRoutedControlNetwork( self, ip='192.168.123.1',
                                       prefixLen=16 ):
        """Configure a routed control network on controller and switches.
           For use with the user datapath only right now."""
        controller = self.controllers[ 0 ]
        info( controller.name + ' <->' )
        cip = ip
        snum = ipParse( ip )
        for switch in self.switches:
            info( ' ' + switch.name )
            link = self.link( switch, controller, port1=0 )
            sintf, cintf = link.intf1, link.intf2
            switch.controlIntf = sintf
            snum += 1
            while snum & 0xff in [ 0, 255 ]:
                snum += 1
            sip = ipStr( snum )
            cintf.setIP( cip, prefixLen )
            sintf.setIP( sip, prefixLen )
            controller.setHostRoute( sip, cintf )
            switch.setHostRoute( cip, sintf )
        info( '\n' )
        info( '*** Testing control network\n' )
        while not cintf.isUp():
            info( '*** Waiting for', cintf, 'to come up\n' )
            sleep( 1 )
        for switch in self.switches:
            while not sintf.isUp():
                info( '*** Waiting for', sintf, 'to come up\n' )
                sleep( 1 )
            if self.ping( hosts=[ switch, controller ] ) != 0:
                error( '*** Error: control network test failed\n' )
                exit( 1 )
        info( '\n' )
