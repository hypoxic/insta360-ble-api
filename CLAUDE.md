# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Python implementation of the Insta360 camera WiFi API, reverse-engineered from the Android app. The project enables programmatic control of Insta360 cameras (tested on ONE RS) through TCP socket communication on port 6666.

## Key Architecture

### Core Components

1. **insta360.py**: Main API implementation
   - Asynchronous TCP socket communication with 12-byte header protocol
   - Background threads for sending/receiving messages
   - Protobuf-based message serialization
   - Queue-based message routing system

2. **pb2/**: Protocol Buffer definitions (108 files)
   - Generated from extracted .proto files from libOne.so
   - Each file defines messages for specific camera operations
   - Import pattern: `from pb2.<module>_pb2 import <MessageClass>`

3. **insta360-remote**: Terminal UI application
   - Uses Python curses for interactive control
   - State machine design with different screens (main menu, settings, etc.)
   - Real-time camera status updates via async callbacks

### Message Flow

1. Client creates request message (protobuf)
2. Wraps in 12-byte header: `[msg_id:4][seq_no:4][msg_type:2][msg_len:2]`
3. Sends over TCP socket
4. Receives response with same header structure
5. Routes response to appropriate callback based on msg_id

## Development Commands

### Running the Applications

```bash
# Run the remote control UI with WiFi (default)
./insta360-remote -i <camera_ip>

# Run the remote control UI with BLE
./insta360-remote -t ble
./insta360-remote -t ble -b <ble_address>  # Specify device address

# Run basic test script
./insta360-test <camera_ip>
```

### Using the Virtual Environment

The project includes a virtual environment with all dependencies pre-installed:

```bash
# Activate the virtual environment
source .venv/bin/activate

# Dependencies (protobuf, bleak) are already installed
# You can now run the scripts with BLE support
```

### Working with Protocol Buffers

```bash
# If you need to regenerate protobuf Python files:
cd utils
protoc --python_out=../pb2 *.proto
```

### Testing Changes

Since there's no formal test suite, test changes by:
1. Running `./insta360-test <camera_ip>` to verify basic operations
2. Testing specific features with `./insta360-remote -i <camera_ip>`
3. Add test cases to `insta360-test` for new functionality

## Transport Layer

The project now supports multiple transport methods through an abstraction layer:

### Transport Classes (transport.py)

1. **TransportBase**: Abstract base class defining the transport interface
2. **WiFiTransport**: TCP socket implementation (port 6666)
3. **BLETransport**: Bluetooth LE implementation using bleak library

### Using Different Transports

```python
# WiFi transport
cam = insta360.camera(host='192.168.42.1', transport='wifi')

# BLE transport
cam = insta360.camera(transport='ble', device_address='XX:XX:XX:XX:XX:XX')
cam = insta360.camera(transport='ble')  # Will scan for devices
```

The protocol messages remain identical across transports - only the delivery mechanism changes.

## Important Implementation Notes

### Adding New Camera Operations

1. Find the appropriate protobuf message in `pb2/`
2. Add method to `Insta360` class following existing patterns:
   ```python
   def NewOperation(self):
       msg = pb2.RequestMessage()
       # Configure message fields
       self._SendMsg(MessageType, msg)
   ```

3. Register callback handler in `_InitCallbacks()` if expecting response

### Handling Camera Responses

- Responses are routed through `_ReceiveCallback()` based on message type
- Add new handlers to `self.callbacks` dictionary in `_InitCallbacks()`
- Use `_logger` for debugging (logs to `remote.log`)

### UI Development (insta360-remote)

- Follow curses best practices: always handle window resize
- Use `addstr()` method which safely handles window boundaries
- State changes through `self.current_window` variable
- Add new menu items to appropriate window drawing methods

## Security Considerations

The camera uses hardcoded WiFi password "88888888" which cannot be changed. This is a known security issue that allows root telnet access to the camera's Linux system. Always use on isolated networks.

## Debugging

- Enable verbose logging by checking `self.verbose` flag
- Logs written to `remote.log` when using insta360-remote
- Use `_DumpBuffer()` to inspect raw protocol messages
- Monitor `self._send_q` and response queues for message flow issues