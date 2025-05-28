# Polar Cloud Interface for Moonraker
#
# Copyright (C) 2025 Moonraker Development Team
# Derived from OctoPrint-PolarCloud by Mark Walker
# 
# This file may be distributed under the terms of the GNU GPLv3 license

from __future__ import annotations
import logging
import asyncio
import time
import json
import base64
import socketio
import uuid
import os
import hashlib
import tempfile
from typing import TYPE_CHECKING, Any, Optional, Dict, List, Union
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes
from ...utils import ServerError
from ...confighelper import ConfigHelper

if TYPE_CHECKING:
    from ...server import Server
    from ..klippy_connection import KlippyConnection
    from ..data_store import DataStore
    from ..file_manager.file_manager import FileManager
    from ...common import WebRequest

# Constants
POLAR_CLOUD_VERSION = "1.0.0"
DEFAULT_CLOUD_URL = "https://printer4.polar3d.com"
REGISTRATION_TIMEOUT = 60
RECONNECT_INTERVAL = 30

class PolarCloud:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.eventloop = self.server.get_event_loop()
        
        # Get config options
        self.service_url = config.get('url', DEFAULT_CLOUD_URL)
        self.serial_number = config.get('serial', None)
        self.enable_debug = config.getboolean('enable_debug', False)
        self.camera_url = config.get('camera_url', None)
        self.printer_type = config.get('printer_type', "cartesian")
        
        # Initialize state
        self.socket: Optional[socketio.AsyncClient] = None
        self.is_connected: bool = False
        self.is_registered: bool = False
        self._challenge: Optional[bytes] = None
        self.last_status: Dict[str, Any] = {}
        self.private_key: Optional[rsa.RSAPrivateKey] = None
        self.public_key: Optional[rsa.RSAPublicKey] = None
        self.registration_future: Optional[asyncio.Future] = None
        self.reconnect_task: Optional[asyncio.Task] = None
        
        # Setup status update timer
        self.status_update_timer = self.eventloop.register_timer(
            self._status_update_handler
        )
        
        # Register server endpoints
        self.server.register_endpoint(
            "/server/polar/register", 
            ["POST"],
            self._handle_register,
            auth_required=True
        )
        self.server.register_endpoint(
            "/server/polar/status",
            ["GET"],
            self._handle_status_request,
            auth_required=True
        )
        self.server.register_endpoint(
            "/server/polar/unregister",
            ["POST"],
            self._handle_unregister,
            auth_required=True
        )
        
        # Register notification handlers
        self.server.register_notification("polar_cloud:status")
        
        if self.serial_number:
            self.eventloop.register_callback(self._connect)

    def _generate_keypair(self) -> None:
        """Generate a new RSA keypair for secure communication"""
        self.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )
        self.public_key = self.private_key.public_key()

    def _get_public_key_pem(self) -> str:
        """Get the public key in PEM format"""
        if not self.public_key:
            self._generate_keypair()
        assert self.public_key is not None
        return base64.b64encode(
            self.public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).decode('utf-8')

    async def _connect(self) -> None:
        if self.socket is not None:
            return
            
        self.socket = socketio.AsyncClient(
            logger=self.enable_debug,
            engineio_logger=self.enable_debug,
            reconnection=True,
            reconnection_attempts=0,  # Infinite retries
            reconnection_delay=5,
            reconnection_delay_max=60
        )
        
        # Register socket events
        self.socket.on('connect', self._on_connect)
        self.socket.on('disconnect', self._on_disconnect)
        self.socket.on('welcome', self._on_welcome)
        self.socket.on('print', self._on_print_request)
        self.socket.on('pause', self._on_pause_request)
        self.socket.on('resume', self._on_resume_request)
        self.socket.on('cancel', self._on_cancel_request)
        self.socket.on('upload', self._on_upload_request)
        self.socket.on('download', self._on_download_request)
        self.socket.on('command', self._on_command_request)
        self.socket.on('error', self._on_error)
        
        try:
            await self.socket.connect(self.service_url)
        except Exception:
            logging.exception("Error connecting to Polar Cloud")
            self.socket = None
            # Schedule reconnection
            if self.reconnect_task is None or self.reconnect_task.done():
                self.reconnect_task = self.eventloop.create_task(self._reconnect_handler())

    async def _reconnect_handler(self) -> None:
        """Handle reconnection attempts"""
        while True:
            await asyncio.sleep(RECONNECT_INTERVAL)
            if self.socket is None or not self.socket.connected:
                try:
                    await self._connect()
                    if self.socket and self.socket.connected:
                        break
                except Exception:
                    logging.exception("Reconnection attempt failed")

    async def _status_update_handler(self, eventtime: float) -> float:
        if not self.is_connected or not self.is_registered:
            return eventtime + 60.0
        
        try:
            klippy: KlippyConnection = self.server.lookup_component('klippy_connection')
            data_store: DataStore = self.server.lookup_component('data_store')
            
            # Get printer status
            status = await klippy.get_klippy_info()
            temperatures = await data_store.get_temperature_store()
            
            # Get print progress if printing
            print_stats = status.get("print_stats", {})
            progress = 0
            if print_stats.get("state", "") == "printing":
                progress = print_stats.get("progress", 0) * 100
            
            # Build status update
            update = {
                "serialNumber": self.serial_number,
                "status": status["state_message"],
                "temperatures": {
                    "bed": temperatures.get("heater_bed", {}).get("temperature", 0),
                    "tool0": temperatures.get("extruder", {}).get("temperature", 0)
                },
                "progress": progress,
                "protocol": "2",
                "camera": self.camera_url,
                "job": {
                    "file": print_stats.get("filename", ""),
                    "estimatedPrintTime": print_stats.get("print_duration", 0),
                    "progress": progress
                }
            }
            
            if self.socket is not None and self.socket.connected:
                await self.socket.emit("status", update)
                self.last_status = update
                # Notify local clients
                self.server.send_event("polar_cloud:status", update)
                
        except Exception:
            logging.exception("Error sending status update")
            
        # Update every 10 seconds when printing, 60 seconds when idle
        is_printing = status.get("print_stats", {}).get("state", "") == "printing"
        return eventtime + (10.0 if is_printing else 60.0)

    async def _on_connect(self) -> None:
        """Handle socket connection"""
        logging.info("Connected to Polar Cloud")
        self.is_connected = True
        if self.serial_number:
            # Re-authenticate if we have a serial number
            await self._authenticate()

    async def _on_disconnect(self) -> None:
        """Handle socket disconnection"""
        logging.info("Disconnected from Polar Cloud")
        self.is_connected = False
        self.is_registered = False
        # Schedule reconnection
        if self.reconnect_task is None or self.reconnect_task.done():
            self.reconnect_task = self.eventloop.create_task(self._reconnect_handler())

    async def _authenticate(self) -> None:
        """Authenticate with the Polar Cloud using the serial number"""
        if not self.socket or not self.socket.connected:
            return
        
        try:
            await self.socket.emit("authenticate", {
                "serialNumber": self.serial_number,
                "protocol": "2"
            })
        except Exception:
            logging.exception("Authentication failed")

    async def _on_welcome(self, data: Dict[str, Any]) -> None:
        """Handle welcome message from server"""
        self.is_registered = True
        if self.registration_future and not self.registration_future.done():
            self.registration_future.set_result(data)

    async def _handle_register(self, web_request: WebRequest) -> Dict[str, Any]:
        email = web_request.get_str('email')
        pin = web_request.get_str('pin')
        
        if not email or not pin:
            raise self.server.error("Missing email or PIN")
            
        if self.socket is None:
            await self._connect()
            
        if self.socket is None:
            raise self.server.error("Unable to connect to Polar Cloud")
            
        # Generate new keypair
        self._generate_keypair()
        
        # Create registration future
        self.registration_future = self.eventloop.create_future()
        
        # Send registration request
        await self.socket.emit("register", {
            "mfg": "moonraker",
            "email": email,
            "pin": pin,
            "myInfo": {
                "protocolVersion": "2",
                "machineType": self.printer_type,
                "publicKey": self._get_public_key_pem()
            }
        })
        
        try:
            # Wait for registration response
            reg_data = await asyncio.wait_for(
                self.registration_future,
                timeout=REGISTRATION_TIMEOUT
            )
            
            # Store serial number
            self.serial_number = reg_data.get("serialNumber")
            if not self.serial_number:
                raise self.server.error("Registration failed: No serial number received")
                
            # Update config
            self.server.write_config_item("polar_cloud", "serial", self.serial_number)
            
            return {
                "status": "registered",
                "serial": self.serial_number
            }
            
        except asyncio.TimeoutError:
            raise self.server.error(
                "Registration timed out after {REGISTRATION_TIMEOUT} seconds"
            )
        except Exception as e:
            raise self.server.error(f"Registration failed: {str(e)}")

    async def _handle_unregister(self, web_request: WebRequest) -> Dict[str, Any]:
        """Handle unregistration request"""
        if not self.serial_number:
            return {"status": "not_registered"}
            
        # Clear registration
        self.serial_number = None
        self.is_registered = False
        self.server.write_config_item("polar_cloud", "serial", None)
        
        # Disconnect from cloud
        if self.socket and self.socket.connected:
            await self.socket.disconnect()
        
        return {"status": "unregistered"}

    async def _handle_status_request(self, web_request: WebRequest) -> Dict[str, Any]:
        return {
            "connected": self.is_connected,
            "registered": self.is_registered,
            "serial": self.serial_number,
            "last_status": self.last_status,
            "camera_url": self.camera_url,
            "printer_type": self.printer_type
        }

    async def _on_print_request(self, data: Dict[str, Any]) -> None:
        """Handle print request from Polar Cloud"""
        try:
            file_manager: FileManager = self.server.lookup_component('file_manager')
            klippy: KlippyConnection = self.server.lookup_component('klippy_connection')
            
            # Download the file if needed
            filename = data.get("filename")
            if not filename:
                raise ValueError("No filename provided")
                
            if "url" in data:
                # Download from URL
                temp_file = await file_manager.download_file(
                    data["url"],
                    filename,
                    "gcodes"
                )
                filename = temp_file
            
            # Start the print
            await klippy.start_print(filename)
            
        except Exception as e:
            logging.exception("Failed to handle print request")
            if self.socket and self.socket.connected:
                await self.socket.emit("error", {
                    "message": f"Print failed: {str(e)}",
                    "code": "print_failed"
                })

    async def _on_pause_request(self, data: Dict[str, Any]) -> None:
        """Handle pause request from Polar Cloud"""
        try:
            klippy: KlippyConnection = self.server.lookup_component('klippy_connection')
            await klippy.pause_print()
        except Exception as e:
            logging.exception("Failed to pause print")
            if self.socket and self.socket.connected:
                await self.socket.emit("error", {
                    "message": f"Pause failed: {str(e)}",
                    "code": "pause_failed"
                })

    async def _on_resume_request(self, data: Dict[str, Any]) -> None:
        """Handle resume request from Polar Cloud"""
        try:
            klippy: KlippyConnection = self.server.lookup_component('klippy_connection')
            await klippy.resume_print()
        except Exception as e:
            logging.exception("Failed to resume print")
            if self.socket and self.socket.connected:
                await self.socket.emit("error", {
                    "message": f"Resume failed: {str(e)}",
                    "code": "resume_failed"
                })

    async def _on_cancel_request(self, data: Dict[str, Any]) -> None:
        """Handle cancel request from Polar Cloud"""
        try:
            klippy: KlippyConnection = self.server.lookup_component('klippy_connection')
            await klippy.cancel_print()
        except Exception as e:
            logging.exception("Failed to cancel print")
            if self.socket and self.socket.connected:
                await self.socket.emit("error", {
                    "message": f"Cancel failed: {str(e)}",
                    "code": "cancel_failed"
                })

    async def _on_upload_request(self, data: Dict[str, Any]) -> None:
        """Handle file upload request from Polar Cloud"""
        try:
            file_manager: FileManager = self.server.lookup_component('file_manager')
            
            filename = data.get("filename")
            file_content = data.get("content")
            
            if not filename or not file_content:
                raise ValueError("Missing filename or content")
            
            # Decode base64 content if needed
            if isinstance(file_content, str):
                file_content = base64.b64decode(file_content)
            
            # Write file
            root = file_manager.get_directory("gcodes")
            file_path = os.path.join(root, filename)
            
            with open(file_path, "wb") as f:
                f.write(file_content)
            
            # Notify success
            if self.socket and self.socket.connected:
                await self.socket.emit("upload_complete", {
                    "filename": filename
                })
                
        except Exception as e:
            logging.exception("Failed to handle file upload")
            if self.socket and self.socket.connected:
                await self.socket.emit("error", {
                    "message": f"Upload failed: {str(e)}",
                    "code": "upload_failed"
                })

    async def _on_download_request(self, data: Dict[str, Any]) -> None:
        """Handle file download request from Polar Cloud"""
        try:
            file_manager: FileManager = self.server.lookup_component('file_manager')
            
            filename = data.get("filename")
            if not filename:
                raise ValueError("No filename provided")
            
            # Read file
            root = file_manager.get_directory("gcodes")
            file_path = os.path.join(root, filename)
            
            with open(file_path, "rb") as f:
                content = f.read()
            
            # Send file content
            if self.socket and self.socket.connected:
                await self.socket.emit("file_content", {
                    "filename": filename,
                    "content": base64.b64encode(content).decode('utf-8')
                })
                
        except Exception as e:
            logging.exception("Failed to handle file download")
            if self.socket and self.socket.connected:
                await self.socket.emit("error", {
                    "message": f"Download failed: {str(e)}",
                    "code": "download_failed"
                })

    async def _on_command_request(self, data: Dict[str, Any]) -> None:
        """Handle custom command request from Polar Cloud"""
        try:
            klippy: KlippyConnection = self.server.lookup_component('klippy_connection')
            command = data.get("command")
            
            if not command:
                raise ValueError("No command provided")
            
            # Execute command
            await klippy.gcode_script(command)
            
        except Exception as e:
            logging.exception("Failed to execute command")
            if self.socket and self.socket.connected:
                await self.socket.emit("error", {
                    "message": f"Command failed: {str(e)}",
                    "code": "command_failed"
                })

    async def _on_error(self, data: Dict[str, Any]) -> None:
        """Handle error messages from the Polar Cloud"""
        error_msg = data.get("message", "Unknown error")
        error_code = data.get("code", "unknown")
        logging.error(f"Polar Cloud error: [{error_code}] {error_msg}")

def load_component(config: ConfigHelper) -> PolarCloud:
    return PolarCloud(config)