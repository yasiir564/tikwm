from flask import Flask, request, jsonify, make_response
import requests
import re
import logging
import json
import os
import time
from functools import wraps
from flask_cors import CORS

app = Flask(__name__)
# Configure CORS properly to allow requests from any origin
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization", "X-Requested-With"]
    }
})

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if os.environ.get('DEBUG') == 'true' else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('tiktok_downloader')

# Cache configuration
CACHE_DIR = 'tiktok_cache'
CACHE_EXPIRY = 3600  # Cache expires after 1 hour (in seconds)

# Create cache directory if it doesn't exist
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

def cache_result(func):
    """
    Decorator to cache API results
    """
    @wraps(func)
    def wrapper(url, *args, **kwargs):
        # Create a cache key from the URL
        cache_key = re.sub(r'[^a-zA-Z0-9]', '_', url)
        cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
        
        # Check if cache exists and is valid
        if os.path.exists(cache_file):
            file_age = time.time() - os.path.getmtime(cache_file)
            if file_age < CACHE_EXPIRY:
                try:
                    with open(cache_file, 'r') as f:
                        logger.info(f"Using cached result for {url}")
                        return json.load(f)
                except (json.JSONDecodeError, IOError) as e:
                    logger.error(f"Error reading cache: {e}")
        
        # If no valid cache, call the original function
        result = func(url, *args, **kwargs)
        
        # Cache the result if successful
        if result and 'video_url' in result:
            try:
                with open(cache_file, 'w') as f:
                    json.dump(result, f)
                    logger.info(f"Cached result for {url}")
            except IOError as e:
                logger.error(f"Error writing cache: {e}")
        
        return result
    
    return wrapper

def extract_tiktok_id(url):
    """
    Extract TikTok video ID from URL
    """
    logger.debug(f"Extracting ID from: {url}")
    
    # Normalize URL
    normalized_url = url
    normalized_url = normalized_url.replace('m.tiktok.com', 'www.tiktok.com')
    normalized_url = normalized_url.replace('vm.tiktok.com', 'www.tiktok.com')
    
    # Regular expressions to match different TikTok URL formats
    patterns = [
        r'tiktok\.com\/@[\w\.]+\/video\/(\d+)',  # Standard format
        r'tiktok\.com\/t\/(\w+)',                # Short URL format
        r'v[mt]\.tiktok\.com\/(\w+)',            # Very short URL format
        r'tiktok\.com\/.*[?&]item_id=(\d+)',     # Query parameter format
    ]
    
    # First try with normalized URL
    for pattern in patterns:
        match = re.search(pattern, normalized_url)
        if match:
            return match.group(1)
    
    # If nothing matched, try with original URL
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    # For vm.tiktok.com and other short URLs - always follow redirect
    if ('vm.tiktok.com' in url or 'vt.tiktok.com' in url or 
            len(url.split('//')[1].split('/')[0]) < 15):
        return 'follow_redirect'
    
    return None

def follow_tiktok_redirects(url):
    """
    Follow redirects to get final URL
    """
    logger.info(f'Following redirects for: {url}')
    
    try:
        response = requests.head(
            url, 
            allow_redirects=True,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            },
            timeout=10
        )
        final_url = response.url
        logger.info(f'Redirect resolved to: {final_url}')
        return final_url
    except Exception as e:
        logger.error(f'Error following redirect: {e}')
        return url

@cache_result
def fetch_from_tikwm(url):
    """
    Try to download TikTok video using TikWM API
    """
    logger.info(f'Using TikWM API service for: {url}')
    
    api_url = 'https://www.tikwm.com/api/'
    
    try:
        response = requests.post(
            api_url,
            data={
                'url': url,
                'hd': 1
            },
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            },
            timeout=30
        )
        
        if response.status_code != 200:
            logger.error(f'Error: TikWM API request failed with status: {response.status_code}')
            return None
        
        data = response.json()
        
        if not data.get('data') or data.get('code') != 0:
            logger.error(f'TikWM API returned error: {data}')
            return None
        
        video_data = data['data']
        
        return {
            'video_url': video_data['play'],       # No watermark
            'cover_url': video_data['cover'],      # Cover image
            'author': video_data['author']['unique_id'],  # Username
            'desc': video_data['title'],           # Video description/title
            'video_id': video_data['id'],          # Video ID
            'method': 'tikwm'
        }
    except Exception as e:
        logger.error(f'Error with TikWM API: {e}')
        return None

def add_cors_headers(response):
    """
    Add CORS headers to a response
    """
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    response.headers.add('Access-Control-Max-Age', '86400')  # 24 hours
    return response

# Explicitly handle preflight OPTIONS requests for all routes
@app.route('/', methods=['OPTIONS'])
@app.route('/api/tiktok/v1/download', methods=['OPTIONS'])
@app.route('/tiktok/v1/download', methods=['OPTIONS'])  # Include the path without /api prefix
def options_handler():
    response = make_response()
    return add_cors_headers(response)

@app.route('/api/tiktok/v1/download', methods=['POST'])
@app.route('/tiktok/v1/download', methods=['POST'])  # Alternative route without /api prefix
def download_tiktok():
    """
    REST API endpoint to download TikTok videos
    """
    logger.info('TikTok downloader request received')
    
    # Get request body
    try:
        params = request.get_json()
        if not params:
            # Try to get data from form if JSON fails
            params = request.form.to_dict()
    except Exception as e:
        logger.error(f'Error parsing request data: {e}')
        response = jsonify({
            'success': False,
            'error': 'Invalid request data'
        })
        return add_cors_headers(response), 400
    
    # Validate URL parameter
    if not params or not params.get('url'):
        logger.error('Error: TikTok URL is missing')
        response = jsonify({
            'success': False,
            'error': 'TikTok URL is required.'
        })
        return add_cors_headers(response), 400
    
    tiktok_url = params['url'].strip()
    logger.info(f'TikTok URL received: {tiktok_url}')
    
    # Handle redirects for short URLs
    if ('vm.tiktok.com' in tiktok_url or 
            'vt.tiktok.com' in tiktok_url or
            len(tiktok_url.split('//')[1].split('/')[0]) < 15):
        
        final_url = follow_tiktok_redirects(tiktok_url)
        logger.info(f'Followed URL redirect to: {final_url}')
        tiktok_url = final_url
    
    # Try to get the video info using TikWM API
    tikwm_result = fetch_from_tikwm(tiktok_url)
    
    if tikwm_result and tikwm_result.get('video_url'):
        logger.info('Successfully extracted video info using TikWM API')
        
        response = jsonify({
            'success': True,
            'video_url': tikwm_result['video_url'],
            'cover_url': tikwm_result.get('cover_url', ''),
            'author': tikwm_result.get('author', ''),
            'desc': tikwm_result.get('desc', ''),
            'video_id': tikwm_result.get('video_id', ''),
            'method': 'tikwm'
        })
        
        return add_cors_headers(response), 200
    
    # If method fails, return error
    logger.error(f'Error: TikWM download method failed for URL: {tiktok_url}')
    response = jsonify({
        'success': False,
        'error': 'Failed to download TikTok video. Please try a different video or URL format.'
    })
    return add_cors_headers(response), 500

# Add a separate route for the root path
@app.route('/', methods=['GET'])
def index():
    response = jsonify({
        'status': 'running',
        'message': 'TikTok Downloader API is active',
        'endpoints': {
            '/api/tiktok/v1/download': 'POST - Download TikTok videos',
            '/tiktok/v1/download': 'POST - Alternative endpoint for downloading TikTok videos',
            '/health': 'GET - Health check endpoint'
        }
    })
    return add_cors_headers(response), 200

# Add a health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    response = jsonify({
        'status': 'healthy',
        'timestamp': int(time.time()),
        'version': '1.0.0'
    })
    return add_cors_headers(response), 200

# Add a catch-all route handler for undefined routes
@app.route('/<path:undefined_route>', methods=['GET', 'POST', 'OPTIONS'])
def handle_undefined_route(undefined_route):
    if request.method == 'OPTIONS':
        response = make_response()
        return add_cors_headers(response)
    
    response = jsonify({
        'success': False,
        'error': f'Route /{undefined_route} not found',
        'available_endpoints': [
            '/api/tiktok/v1/download (POST)',
            '/tiktok/v1/download (POST)',
            '/health (GET)'
        ]
    })
    return add_cors_headers(response), 404

# Error handlers
@app.errorhandler(404)
def not_found(error):
    response = jsonify({
        'success': False,
        'error': 'Not found'
    })
    return add_cors_headers(response), 404

@app.errorhandler(500)
def server_error(error):
    response = jsonify({
        'success': False,
        'error': 'Internal server error'
    })
    return add_cors_headers(response), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('DEBUG') == 'true'
    
    logger.info(f'Starting TikTok Downloader API on port {port} (debug={debug_mode})')
    app.run(debug=debug_mode, host='0.0.0.0', port=port)
