# Polar Cloud Component

The Polar Cloud component allows you to connect your Moonraker instance to the Polar3D cloud service, enabling remote monitoring and control of your 3D printer from anywhere via https://polar3d.com.

## Configuration

Add the following section to your `moonraker.conf` file:

```ini
[polar_cloud]
url: https://printer4.polar3d.com  # Optional - defaults to https://printer4.polar3d.com
enable_debug: False                # Optional - enable debug logging
camera_url:                       # Optional - URL to your printer's camera stream
printer_type: cartesian           # Optional - printer type (cartesian or delta)
```

## Setup

1. Create a Polar Cloud account at https://polar3d.com if you don't have one already
2. Set up a PIN in your Polar Cloud account settings (click on your portrait and choose Settings)
3. Register your printer with Polar Cloud:
   ```bash
   curl -H "Content-Type: application/json" -X POST \
        -d '{"email":"your.email@example.com","pin":"your-pin"}' \
        http://localhost:7125/server/polar/register
   ```
   Replace `your.email@example.com` and `your-pin` with your Polar Cloud credentials.

4. The registration process will return a serial number that will be automatically saved in your configuration.

## Features

- Remote monitoring of printer status
- Remote control (start/pause/resume/cancel prints)
- File upload/download
- Camera stream viewing (if configured)
- Custom G-code command execution
- Automatic reconnection on connection loss
- Secure communication using RSA encryption

## API Endpoints

### Register Printer
- **URL:** `/server/polar/register`
- **Method:** `POST`
- **Data:**
  ```json
  {
    "email": "your.email@example.com",
    "pin": "your-pin"
  }
  ```
- **Response:**
  ```json
  {
    "status": "registered",
    "serial": "XXXX-XXXX-XXXX"
  }
  ```

### Get Status
- **URL:** `/server/polar/status`
- **Method:** `GET`
- **Response:**
  ```json
  {
    "connected": true,
    "registered": true,
    "serial": "XXXX-XXXX-XXXX",
    "last_status": {
      "status": "printing",
      "progress": 45.2,
      ...
    },
    "camera_url": "http://...",
    "printer_type": "cartesian"
  }
  ```

### Unregister Printer
- **URL:** `/server/polar/unregister`
- **Method:** `POST`
- **Response:**
  ```json
  {
    "status": "unregistered"
  }
  ```

## Notifications

The component sends the following notifications that can be subscribed to by clients:

- `polar_cloud:status` - Sent when printer status is updated

## Troubleshooting

1. **Connection Issues**
   - Check if your Moonraker instance has internet access
   - Verify your Polar Cloud credentials
   - Check the Moonraker logs for detailed error messages

2. **Camera Stream Not Working**
   - Ensure your camera URL is accessible from outside your network
   - Use an absolute URL for the camera stream
   - Check if your camera stream format is supported (MJPEG recommended)

3. **File Upload/Download Issues**
   - Check if you have sufficient disk space
   - Verify file permissions in your gcode directory
   - Check file size limits

## Credits

This implementation is based on the [OctoPrint-PolarCloud](https://github.com/markwal/OctoPrint-PolarCloud) plugin by Mark Walker. 