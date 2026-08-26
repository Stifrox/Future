import sys

import uvicorn
import api_server

uvicorn.run(api_server.app, host="0.0.0.0", port=8000)
