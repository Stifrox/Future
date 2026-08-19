import sys

import uvicorn
import api_server

uvicorn.run(api_server.app, host="127.0.0.1", port=8000)
