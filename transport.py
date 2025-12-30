# -*- coding: utf-8 -*-
"""
Transport layer abstraction for Insta360 camera communication.

This module provides a unified interface for different transport mechanisms
(WiFi TCP sockets, Bluetooth LE) to communicate with Insta360 cameras.
"""

from abc import ABC, abstractmethod
import asyncio
import logging
import socket
import struct
import threading
import time
from typing import Optional, Callable, Tuple

import select

__author__ = "Niccolo Rigacci"
__copyright__ = "Copyright 2024 Niccolo Rigacci <niccolo@rigacci.org>"
__license__ = "GPLv3-or-later"
__email__ = "niccolo@rigacci.org"
__version__ = "0.2.0"


class TransportBase(ABC):
    """Abstract base class for all transport implementations."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.is_connected = False
        self.receive_callback: Optional[Callable[[bytes], None]] = None
        
    @abstractmethod
    def connect(self, **kwargs) -> bool:
        """Connect to the camera. Returns True on success."""
        pass
    
    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect from the camera."""
        pass
    
    @abstractmethod
    def send(self, data: bytes) -> bool:
        """Send raw data to the camera. Returns True on success."""
        pass
    
    @abstractmethod
    def start_receiving(self, callback: Callable[[bytes], None]) -> None:
        """Start receiving data asynchronously. Callback receives raw bytes."""
        pass
    
    @abstractmethod
    def stop_receiving(self) -> None:
        """Stop receiving data."""
        pass
    
    @property
    @abstractmethod
    def connection_info(self) -> str:
        """Return human-readable connection information."""
        pass


class WiFiTransport(TransportBase):
    """WiFi/TCP socket transport implementation."""
    
    SOCKET_TIMEOUT_SEC = 5.0
    PKT_COMPLETE_TIMEOUT_SEC = 4.0
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        super().__init__(logger)
        self.host: Optional[str] = None
        self.port: Optional[int] = None
        self.socket: Optional[socket.socket] = None
        self.socket_lock = threading.Lock()
        self.rcv_thread: Optional[threading.Thread] = None
        self.rcv_buffer = b''
        self.stop_receiving_flag = False
        
    def connect(self, host: str = '192.168.42.1', port: int = 6666, **kwargs) -> bool:
        """Connect to camera via TCP socket."""
        try:
            self.host = host
            self.port = port
            
            self.logger.info(f'WiFi: Connecting to {host}:{port}')
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(self.SOCKET_TIMEOUT_SEC)
            self.socket.connect((host, port))
            
            self.is_connected = True
            self.logger.debug('WiFi: Socket connected')
            return True
            
        except Exception as e:
            self.logger.error(f'WiFi: Connection failed: {e}')
            self.socket = None
            self.is_connected = False
            return False
    
    def disconnect(self) -> None:
        """Close the TCP socket."""
        self.logger.debug('WiFi: Disconnecting')
        self.stop_receiving()
        
        if self.socket:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
                self.socket.close()
            except:
                pass
            self.socket = None
            
        self.is_connected = False
        self.rcv_buffer = b''
        
    def send(self, data: bytes) -> bool:
        """Send data through the socket."""
        if not self.socket or not self.is_connected:
            return False
            
        try:
            with self.socket_lock:
                self.socket.sendall(data)
            return True
        except Exception as e:
            self.logger.error(f'WiFi: Send failed: {e}')
            return False
    
    def start_receiving(self, callback: Callable[[bytes], None]) -> None:
        """Start the receiving thread."""
        if self.rcv_thread and self.rcv_thread.is_alive():
            return
            
        self.receive_callback = callback
        self.stop_receiving_flag = False
        self.rcv_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.rcv_thread.start()
        
    def stop_receiving(self) -> None:
        """Stop the receiving thread."""
        self.stop_receiving_flag = True
        if self.rcv_thread:
            self.rcv_thread.join(timeout=2.0)
            self.rcv_thread = None
            
    def _receive_loop(self) -> None:
        """Receiving thread main loop."""
        while not self.stop_receiving_flag and self.socket:
            if not self._poll_and_receive():
                time.sleep(0.1)
                
    def _poll_and_receive(self) -> bool:
        """Poll socket and receive data. Returns True if data was received."""
        if not self.socket:
            return False
            
        try:
            poller = select.poll()
            poller.register(self.socket, select.POLLIN)
            events = poller.poll(100)  # 100ms timeout
            
            for sock, event in events:
                if event & select.POLLIN and sock == self.socket.fileno():
                    data = self.socket.recv(4096)
                    if data and self.receive_callback:
                        self.receive_callback(data)
                    return True
                    
        except Exception as e:
            self.logger.error(f'WiFi: Receive error: {e}')
            
        return False
    
    @property
    def connection_info(self) -> str:
        """Return connection information."""
        if self.is_connected and self.host and self.port:
            return f"WiFi {self.host}:{self.port}"
        return "WiFi (disconnected)"


class BLETransport(TransportBase):
    """Bluetooth LE transport implementation."""
    
    # BLE UUIDs
    SERVICE_UUID = "0000be80-0000-1000-8000-00805f9b34fb"
    WRITE_CHAR_UUID = "0000be81-0000-1000-8000-00805f9b34fb"  # write, read
    READ_CHAR_UUID = "0000be82-0000-1000-8000-00805f9b34fb"   # notify
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        super().__init__(logger)
        self.device_address: Optional[str] = None
        self.device_name: Optional[str] = None
        self.client = None  # BleakClient instance
        self.event_loop: Optional[asyncio.AbstractEventLoop] = None
        self.ble_thread: Optional[threading.Thread] = None
        self._connect_future: Optional[asyncio.Future] = None
        self._disconnect_future: Optional[asyncio.Future] = None
        self._pending_tasks: set = set()  # Track pending async tasks
        self._disconnecting = False  # Flag to prevent new operations during disconnect
        
    def connect(self, device_address: Optional[str] = None, scan_timeout: float = None, **kwargs) -> bool:
        """Connect to camera via BLE."""
        try:
            # Import here to make BLE optional
            from bleak import BleakClient, BleakScanner
            
            # Start event loop in separate thread
            self.ble_thread = threading.Thread(target=self._run_event_loop, daemon=True)
            self.ble_thread.start()
            
            # Wait for event loop to start
            time.sleep(0.1)
            
            # Submit connection task
            future = asyncio.run_coroutine_threadsafe(
                self._async_connect(device_address, scan_timeout),
                self.event_loop
            )
            
            # Wait for connection - no timeout if scan_timeout is None
            if scan_timeout is None:
                # No timeout - wait indefinitely
                return future.result()
            else:
                # Wait with timeout
                connection_timeout = scan_timeout + 20  # Give more time for BLE connection
                return future.result(timeout=connection_timeout)
            
        except ImportError:
            self.logger.error('BLE: bleak library not installed. Run: pip install bleak')
            return False
        except Exception as e:
            self.logger.error(f'BLE: Connection failed: {e}')
            return False
    
    def disconnect(self) -> None:
        """Disconnect from BLE device."""
        self._disconnecting = True
        self.is_connected = False
        
        if self.event_loop and self.client:
            future = asyncio.run_coroutine_threadsafe(
                self._async_disconnect(),
                self.event_loop
            )
            try:
                future.result(timeout=5.0)
            except:
                pass
        
        # Cancel all pending tasks
        if self.event_loop:
            def cancel_tasks():
                for task in self._pending_tasks:
                    if not task.done():
                        task.cancel()
                self._pending_tasks.clear()
                
            self.event_loop.call_soon_threadsafe(cancel_tasks)
            # Give tasks a moment to cancel
            time.sleep(0.1)
            
            # Stop event loop
            self.event_loop.call_soon_threadsafe(self.event_loop.stop)
            
        if self.ble_thread:
            self.ble_thread.join(timeout=2.0)
            self.ble_thread = None
            
        self._disconnecting = False
            
    def send(self, data: bytes) -> bool:
        """Send data via BLE."""
        if not self.client or not self.is_connected or not self.event_loop or self._disconnecting:
            return False
        
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_send(data),
                self.event_loop
            )
            return future.result(timeout=2.0)
        except Exception:
            if not self._disconnecting:
                self.logger.error('BLE: Send timeout or error')
            return False
    
    def start_receiving(self, callback: Callable[[bytes], None]) -> None:
        """BLE notifications are started automatically on connect."""
        self.receive_callback = callback
        
    def stop_receiving(self) -> None:
        """BLE notifications are stopped automatically on disconnect."""
        self.receive_callback = None
        
    def _run_event_loop(self) -> None:
        """Run the asyncio event loop in a separate thread."""
        self.event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.event_loop)
        self.event_loop.run_forever()
        
    async def _async_connect(self, device_address: Optional[str], scan_timeout: Optional[float]) -> bool:
        """Async connect implementation."""
        from bleak import BleakClient, BleakScanner
        
        # Find device if address not provided
        if not device_address:
            if scan_timeout is None:
                self.logger.info('BLE: Scanning for Insta360 cameras (no timeout - will scan indefinitely)')
            else:
                self.logger.info(f'BLE: Scanning for Insta360 cameras (timeout={scan_timeout}s)')
            
            device = await self._find_camera(scan_timeout)
            if not device:
                self.logger.error('BLE: No Insta360 camera found')
                return False
            device_address = device.address
            self.device_name = device.name
        
        self.device_address = device_address
        self.logger.info(f'BLE: Connecting to {device_address}')
        
        # Connect
        # BleakClient timeout is set in constructor
        if scan_timeout is None:
            # No timeout - use a very long timeout
            self.client = BleakClient(device_address, timeout=3600.0)  # 1 hour
        else:
            # Normal timeout
            self.client = BleakClient(device_address, timeout=20.0)
            
        try:
            self.logger.debug(f'BLE: Calling connect()...')
            connected = await self.client.connect()
            self.logger.debug(f'BLE: connect() returned: {connected}')
            
            if connected or self.client.is_connected:
                self.logger.info(f'BLE: Connected to device, enabling notifications')
                # Enable notifications
                await self.client.start_notify(self.READ_CHAR_UUID, self._notification_handler)
                self.is_connected = True
                self.logger.info(f'BLE: Successfully connected to {self.device_name or device_address}')
                return True
            else:
                self.logger.error(f'BLE: connect() returned False')
                
        except Exception as e:
            self.logger.error(f'BLE: Connection failed: {e}')
            import traceback
            self.logger.error(f'BLE: Traceback: {traceback.format_exc()}')
            
        return False
    
    async def _async_disconnect(self) -> None:
        """Async disconnect implementation."""
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(self.READ_CHAR_UUID)
                await self.client.disconnect()
            except:
                pass
            
    async def _async_send(self, data: bytes) -> bool:
        """Async send implementation."""
        try:
            if self._disconnecting or not self.client or not self.client.is_connected:
                return False
            await self.client.write_gatt_char(self.WRITE_CHAR_UUID, data)
            return True
        except asyncio.CancelledError:
            # Task was cancelled during disconnect - this is expected
            return False
        except Exception as e:
            # Error 41 is "Characteristic doesn't support write" or connection issues
            if not self._disconnecting and self.client and self.client.is_connected:
                self.logger.error(f'BLE: Send failed: {e}')
            return False
    
    async def _find_camera(self, timeout: Optional[float]):
        """Find first Insta360 camera via BLE scanning."""
        from bleak import BleakScanner
        
        if timeout is not None:
            # Normal scanning with timeout
            devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
            
            for device, adv_data in devices.values():
                service_uuids = [uuid.lower() for uuid in adv_data.service_uuids]
                if self.SERVICE_UUID.lower() in service_uuids:
                    self.logger.info(f'BLE: Found {device.name} ({device.address})')
                    return device
            return None
        else:
            # Continuous scanning until device found
            found_device = None
            scanner = None
            
            def detection_callback(device, adv_data):
                nonlocal found_device, scanner
                # Only log if we haven't found a device yet
                if found_device is None:
                    service_uuids = [uuid.lower() for uuid in adv_data.service_uuids]
                    if self.SERVICE_UUID.lower() in service_uuids:
                        self.logger.info(f'BLE: Found {device.name} ({device.address})')
                        found_device = device
                        # Stop scanning immediately when device is found
                        if scanner:
                            asyncio.create_task(scanner.stop())
                    
            # Keep scanning in chunks until we find a device
            scan_duration = 5.0  # Max scan duration per attempt
            scan_count = 0
            
            while found_device is None:
                scan_count += 1
                if scan_count > 1:
                    self.logger.info(f'BLE: Still scanning... (attempt {scan_count})')
                    
                scanner = BleakScanner(detection_callback=detection_callback)
                await scanner.start()
                
                # Wait for device to be found or timeout
                start_time = asyncio.get_event_loop().time()
                while found_device is None and (asyncio.get_event_loop().time() - start_time) < scan_duration:
                    await asyncio.sleep(0.1)
                
                await scanner.stop()
                
                if found_device:
                    return found_device
                    
            return None
    
    def _notification_handler(self, sender, data: bytes) -> None:
        """Handle BLE notifications."""
        if self.receive_callback:
            self.receive_callback(data)
            
    @property
    def connection_info(self) -> str:
        """Return connection information."""
        if self.is_connected:
            name = self.device_name or self.device_address or "Unknown"
            return f"BLE {name}"
        return "BLE (disconnected)"


class TransportFactory:
    """Factory for creating transport instances."""
    
    @staticmethod
    def create_transport(transport_type: str, logger: Optional[logging.Logger] = None) -> TransportBase:
        """Create a transport instance of the specified type.
        
        Args:
            transport_type: 'wifi' or 'ble'
            logger: Optional logger instance
            
        Returns:
            Transport instance
            
        Raises:
            ValueError: If transport_type is not supported
        """
        transport_type = transport_type.lower()
        
        if transport_type == 'wifi':
            return WiFiTransport(logger)
        elif transport_type == 'ble':
            return BLETransport(logger)
        else:
            raise ValueError(f"Unsupported transport type: {transport_type}")