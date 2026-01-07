import os
import logging
from flask import Flask, render_template, request, jsonify
from app.core import Config, ContentPipeline

app = Flask(__name__, template_folder='app/templates', static_folder='app/static')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process():
    youtube_url = request.form.get('youtube_url')
    if not youtube_url:
        return jsonify({'error': 'YouTube URL is required'}), 400

    try:
        # Initialize config with the provided URL
        config = Config.from_env(youtube_url=youtube_url)
        
        # Run the pipeline
        pipeline = ContentPipeline(config)
        result = pipeline.run()
        
        return render_template('result.html', result=result)
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        return render_template('error.html', error=str(e))

if __name__ == '__main__':
    # Ensure we run on port 5000
    app.run(host='0.0.0.0', port=5000, debug=True)
