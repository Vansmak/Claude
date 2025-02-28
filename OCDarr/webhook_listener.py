from flask import Flask, render_template, request, redirect, url_for, jsonify
import subprocess
import os
import time
import logging
import json
import sonarr_utils
import threading
from datetime import datetime
from dotenv import load_dotenv
import requests  # Add this import statement
from logging.handlers import RotatingFileHandler


app = Flask(__name__)

# Load environment variables from .env file
load_dotenv()

# Load environment variables
SONARR_URL = os.getenv('SONARR_URL')
SONARR_API_KEY = os.getenv('SONARR_API_KEY')
MISSING_LOG_PATH = os.getenv('MISSING_LOG_PATH', '/app/logs/missing.log')

# Setup logging with rotation
logging.basicConfig(
    level=logging.DEBUG if os.getenv('FLASK_DEBUG', 'false').lower() == 'true' else logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Create a rotating file handler for the app log
log_handler = RotatingFileHandler(
    os.getenv('LOG_PATH', '/app/logs/app.log'), 
    maxBytes=10*1024*1024,  # 10MB max size
    backupCount=5           # Keep 5 backup files
)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
app.logger.addHandler(log_handler)

# Adding stream handler to also log to console for Docker logs to capture
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.DEBUG if os.getenv('FLASK_DEBUG', 'false').lower() == 'true' else logging.INFO)
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(message)s')
stream_handler.setFormatter(formatter)
app.logger.addHandler(stream_handler)

# Configuration management
config_path = os.path.join(app.root_path, 'config', 'config.json')
TIMESTAMP_FILE_PATH = '/app/backgrounds/fanart_timestamp.txt'

def load_config():
    try:
        with open(config_path, 'r') as file:
            config = json.load(file)
        if 'rules' not in config:
            config['rules'] = {}
        if 'services' not in config:
            config['services'] = {}
        
        # Ensure default rule is '1n1' if not explicitly specified
        if 'default_rule' not in config:
            config['default_rule'] = '1n1'
            
        return config
    except FileNotFoundError:
        # Default configuration setup
        return {
            'services': {},
            'rules': {
                '1n1': {
                    'get_option': '1',
                    'action_option': 'search',
                    'keep_watched': '1',
                    'monitor_watched': False,
                    'series': []
                }
            },
            'default_rule': '1n1'
        }


# Save configuration function
def save_config(config):
    with open(config_path, 'w') as file:
        json.dump(config, file, indent=4)

def get_missing_log_content():
    try:
        with open(MISSING_LOG_PATH, 'r') as file:
            return file.read()
    except FileNotFoundError:
        return "No missing entries logged."
    except Exception as e:
        app.logger.error(f"Failed to read missing log: {str(e)}")
        return "Failed to read log."

# Load configuration
config = load_config()
services = config.get('services', {})

def check_service_status(url):
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return "Online"
    except requests.exceptions.RequestException:
        return "Offline"
    return "Offline"


@app.route('/')
def home():
    section = request.args.get('section', 'current')
    config = load_config()
    services = config.get('services', {})
    preferences = sonarr_utils.load_preferences()
    current_series = sonarr_utils.fetch_series_and_episodes(preferences)
    upcoming_premieres = sonarr_utils.fetch_upcoming_premieres(preferences)
    
    return render_template(
        'index.html',
        config=config,
        section=section,
        active_section='current',
        current_series=current_series,
        upcoming_premieres=upcoming_premieres,
        sonarr_url=SONARR_URL
    )

@app.route('/settings')
def settings_page():
    config = load_config()
    services = config.get('services', {})
    preferences = sonarr_utils.load_preferences()
    all_series = sonarr_utils.get_series_list(preferences)
    
    # Maintain the rules mapping logic
    rules_mapping = {str(series_id): rule_name for rule_name, details in config['rules'].items() for series_id in details.get('series', [])}
    
    # Assign '1n1' as the default rule if no specific rule is found
    default_rule = config.get('default_rule', '1n1')
    for series in all_series:
        series['assigned_rule'] = rules_mapping.get(str(series['id']), default_rule)

    rule = request.args.get('rule', '1n1')
    message = request.args.get('message')
    service_status = {name: check_service_status(details['url']) for name, details in services.items()}

    return render_template(
        'settings.html',
        config=config,
        active_section='settings',
        all_series=all_series,
        sonarr_url=SONARR_URL,
        rule=rule,
        message=message,
        service_status=service_status,
        services=services
    )

@app.route('/update-settings', methods=['POST'])
def update_settings():
    config = load_config()
    
    rule_name = request.form.get('rule_name')
    if rule_name == 'add_new':
        rule_name = request.form.get('new_rule_name')
        if not rule_name:
            return redirect(url_for('settings_page', message="New rule name is required."))
    
    get_option = request.form.get('get_option')
    keep_watched = request.form.get('keep_watched')

    config['rules'][rule_name] = {
        'get_option': get_option,
        'action_option': request.form.get('action_option'),
        'keep_watched': keep_watched,
        'monitor_watched': request.form.get('monitor_watched', 'false').lower() == 'true',
        'series': config['rules'].get(rule_name, {}).get('series', [])
    }
    
    save_config(config)
    return redirect(url_for('settings_page', message="Settings updated successfully"))

@app.route('/delete_rule', methods=['POST'])
def delete_rule():
    config = load_config()
    rule_name = request.form.get('rule_name')
    if rule_name and rule_name in config['rules']:
        del config['rules'][rule_name]
        save_config(config)
        return redirect(url_for('settings_page', message=f"Rule '{rule_name}' deleted successfully."))
    else:
        return redirect(url_for('settings_page', message=f"Rule '{rule_name}' not found."))

@app.route('/assign_rules', methods=['POST'])
def assign_rules():
    config = load_config()
    rule_name = request.form.get('assign_rule_name')
    submitted_series_ids = set(request.form.getlist('series_ids'))

    if rule_name == 'None':
        # Remove series from any rule
        for key, details in config['rules'].items():
            details['series'] = [sid for sid in details.get('series', []) if sid not in submitted_series_ids]
    else:
        # Update the rule's series list to include only those submitted
        if rule_name in config['rules']:
            current_series = set(config['rules'][rule_name]['series'])
            updated_series = current_series.union(submitted_series_ids)
            config['rules'][rule_name]['series'] = list(updated_series)

        # Update other rules to remove the series if it's no longer assigned there
        for key, details in config['rules'].items():
            if key != rule_name:
                # Preserve series not submitted in other rules
                details['series'] = [sid for sid in details.get('series', []) if sid not in submitted_series_ids]

    save_config(config)
    return redirect(url_for('assign_rules_page', message="Rules updated successfully."))

@app.route('/unassign_rules', methods=['POST'])
def unassign_rules():
    config = load_config()
    rule_name = request.form.get('assign_rule_name')
    submitted_series_ids = set(request.form.getlist('series_ids'))

    # Update the rule's series list to exclude those submitted
    if rule_name in config['rules']:
        current_series = set(config['rules'][rule_name]['series'])
        updated_series = current_series.difference(submitted_series_ids)
        config['rules'][rule_name]['series'] = list(updated_series)

    save_config(config)
    return redirect(url_for('assign_rules_page', message="Rules updated successfully."))

@app.route('/jellyfin-webhook', methods=['POST'])
def handle_jellyfin_webhook():
    app.logger.info("Received Jellyfin webhook")
    data = request.json
    if data:
        try:
            # Check if this is a progress event
            if data.get('NotificationType') != 'PlaybackProgress':
                return jsonify({'status': 'success'}), 200
            
            # Get playback position as percentage
            position_ticks = int(data.get('PlaybackPositionTicks', 0))
            total_ticks = int(data.get('RunTimeTicks', 0))
            
            if total_ticks > 0:
                progress_percent = (position_ticks / total_ticks) * 100
                
                # Only log when we're getting close to the threshold
                if 40 <= progress_percent <= 60:
                    app.logger.info(f"Progress: {progress_percent}%")
                
                # Only proceed if we're between 45-55%
                if 45 <= progress_percent <= 55:
                    app.logger.info(f"Processing {data.get('SeriesName')} S{data.get('SeasonNumber')}E{data.get('EpisodeNumber')}")
                    
                    episode_data = {
                        "server_title": data.get('SeriesName'),
                        "server_season_num": str(data.get('SeasonNumber')),
                        "server_ep_num": str(data.get('EpisodeNumber'))
                    }
                    
                    # Write the data and run the script
                    temp_dir = '/app/temp'
                    os.makedirs(temp_dir, exist_ok=True)
                    with open(os.path.join(temp_dir, 'data_from_server.json'), 'w') as f:
                        json.dump(episode_data, f)
                    
                    result = subprocess.run(["python3", "/app/servertosonarr.py"],
                                       capture_output=True,
                                       text=True)
                    
                    if result.stderr:
                        app.logger.error(f"Script errors: {result.stderr}")
                    
                    app.logger.info("Successfully processed episode")
            
            return jsonify({'status': 'success'}), 200
            
        except Exception as e:
            app.logger.error(f"Error processing webhook: {str(e)}")
            return jsonify({'status': 'error', 'message': str(e)}), 500
    else:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400
  

@app.route('/webhook', methods=['POST'])
def handle_server_webhook():
    app.logger.info("Received POST request from Tautulli")
    data = request.json
    if data:
        app.logger.info(f"Webhook received with data: {data}")
        try:
            temp_dir = '/app/temp'
            os.makedirs(temp_dir, exist_ok=True)
            with open(os.path.join(temp_dir, 'data_from_server.json'), 'w') as f:
                json.dump(data, f)
            app.logger.info("Data successfully written to data_from_server.json")
            
            result = subprocess.run(["python3", "/app/servertosonarr.py"], capture_output=True, text=True)
            if result.stderr:
                app.logger.error("Errors from servertosonarr.py: " + result.stderr)
        except Exception as e:
            app.logger.error(f"Failed to handle data or run script: {e}")
            return jsonify({'status': 'error', 'message': str(e)}), 500
        return jsonify({'status': 'success', 'message': 'Script triggered successfully'}), 200
    else:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

@app.route('/episode-selection')
def episode_selection_form():
    """Display the episode selection form"""
    try:
        series_id = request.args.get('series_id')
        season = request.args.get('season')
        start_ep = request.args.get('start_ep')
        end_ep = request.args.get('end_ep')
        
        if not series_id:
            return redirect(url_for('settings_page'))
            
        config = load_config()
        preferences = sonarr_utils.load_preferences()
        
        # Get series info
        headers = {'X-Api-Key': SONARR_API_KEY}
        series_response = requests.get(f"{SONARR_URL}/api/v3/series/{series_id}", headers=headers)
        
        if not series_response.ok:
            return f"Error: Failed to get series info. Status: {series_response.status_code}"
            
        series = series_response.json()
        
        # If season not provided, show season selection
        if not season:
            return render_template(
                'episode_selection.html',
                series=series,
                season=None,
                episodes=None,
                config=config
            )
            
        # Get episodes for the season
        import episeerr_utils
        episodes = episeerr_utils.get_series_episodes(int(series_id), int(season))
        
        if not episodes:
            return f"Error: No episodes found for {series['title']} Season {season}"
            
        # Sort episodes by episode number
        episodes.sort(key=lambda ep: ep.get('episodeNumber', 0))
        
        # Pre-select episodes if specified
        preselected = []
        if start_ep and end_ep:
            start = int(start_ep)
            end = int(end_ep)
            preselected = list(range(start, end + 1))
        
        return render_template(
            'episode_selection.html',
            series=series,
            season=season,
            episodes=episodes,
            preselected=preselected,
            config=config
        )
    except Exception as e:
        app.logger.error(f"Error loading episode selection: {str(e)}")
        return f"Error: {str(e)}"
    
@app.route('/pending-requests')
def get_pending_requests():
    """Get any pending redirects/requests."""
    pending = app.config.get('PENDING_REDIRECTS', [])
    return jsonify(pending)

@app.route('/search-tvdb')
def search_tvdb():
    """Search for series on TVDB."""
    try:
        query = request.args.get('query')
        if not query:
            return jsonify({'status': 'error', 'message': 'No query provided'}), 400
            
        headers = {'X-Api-Key': SONARR_API_KEY}
        
        # Use Sonarr's lookup endpoint to search TVDB
        response = requests.get(
            f"{SONARR_URL}/api/v3/series/lookup?term={query}",
            headers=headers
        )
        
        if not response.ok:
            return jsonify({'status': 'error', 'message': 'Failed to search TVDB'}), 500
            
        results = response.json()
        
        # Format the results
        formatted_results = []
        for result in results:
            formatted_results.append({
                'tvdbId': result.get('tvdbId'),
                'title': result.get('title'),
                'year': result.get('year'),
                'status': result.get('status'),
                'overview': result.get('overview')
            })
            
        return jsonify({'status': 'success', 'results': formatted_results})
        
    except Exception as e:
        app.logger.error(f"Error searching TVDB: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/add-series', methods=['POST'])
def add_series():
    """Add a series to Sonarr."""
    try:
        data = request.json
        tvdb_id = data.get('tvdbId')
        
        if not tvdb_id:
            return jsonify({'status': 'error', 'message': 'No TVDB ID provided'}), 400
            
        headers = {'X-Api-Key': SONARR_API_KEY}
        
        # Get root folders
        root_folders_response = requests.get(
            f"{SONARR_URL}/api/v3/rootfolder",
            headers=headers
        )
        
        if not root_folders_response.ok:
            return jsonify({'status': 'error', 'message': 'Failed to get root folders'}), 500
            
        root_folders = root_folders_response.json()
        if not root_folders:
            return jsonify({'status': 'error', 'message': 'No root folders configured in Sonarr'}), 500
            
        # Get quality profiles
        profiles_response = requests.get(
            f"{SONARR_URL}/api/v3/qualityprofile",
            headers=headers
        )
        
        if not profiles_response.ok:
            return jsonify({'status': 'error', 'message': 'Failed to get quality profiles'}), 500
            
        profiles = profiles_response.json()
        if not profiles:
            return jsonify({'status': 'error', 'message': 'No quality profiles configured in Sonarr'}), 500
            
        # Use lookup to get full series data
        lookup_response = requests.get(
            f"{SONARR_URL}/api/v3/series/lookup?term=tvdb:{tvdb_id}",
            headers=headers
        )
        
        if not lookup_response.ok:
            return jsonify({'status': 'error', 'message': 'Failed to lookup series'}), 500
            
        lookup_results = lookup_response.json()
        if not lookup_results:
            return jsonify({'status': 'error', 'message': 'No results found for TVDB ID'}), 404
            
        series_to_add = lookup_results[0]
        
        # Add the episodes tag
        if 'tags' not in series_to_add:
            series_to_add['tags'] = []
            
        # Get tag ID for "episodes"
        tag_response = requests.get(
            f"{SONARR_URL}/api/v3/tag",
            headers=headers
        )
        
        if tag_response.ok:
            tags = tag_response.json()
            episodes_tag = next((tag for tag in tags if tag['label'].lower() == 'episodes'), None)
            
            if episodes_tag:
                series_to_add['tags'].append(episodes_tag['id'])
            else:
                # Create the tag if it doesn't exist
                tag_create_response = requests.post(
                    f"{SONARR_URL}/api/v3/tag",
                    headers=headers,
                    json={"label": "episodes"}
                )
                
                if tag_create_response.ok:
                    new_tag = tag_create_response.json()
                    series_to_add['tags'].append(new_tag['id'])
                    
        # Set path, profile, etc.
        series_to_add['rootFolderPath'] = root_folders[0]['path']
        series_to_add['qualityProfileId'] = profiles[0]['id']
        series_to_add['seasonFolder'] = True
        series_to_add['monitored'] = True
        
        # Add the series
        add_response = requests.post(
            f"{SONARR_URL}/api/v3/series",
            headers=headers,
            json=series_to_add
        )
        
        if not add_response.ok:
            return jsonify({'status': 'error', 'message': f'Failed to add series: {add_response.text}'}), 500
            
        added_series = add_response.json()
        
        # Return success with the added series
        return jsonify({
            'status': 'success',
            'message': f'Added {added_series["title"]} to Sonarr',
            'series': {
                'id': added_series['id'],
                'title': added_series['title'],
                'tvdbId': added_series['tvdbId']
            }
        })
        
    except Exception as e:
        app.logger.error(f"Error adding series: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
@app.route('/get-seasons', methods=['GET'])
def get_seasons():
    """Get seasons for a series."""
    try:
        series_id = request.args.get('series_id')
        if not series_id:
            return jsonify({'status': 'error', 'message': 'No series ID provided'}), 400
            
        headers = {'X-Api-Key': SONARR_API_KEY}
        
        # Get series info for seasons
        response = requests.get(f"{SONARR_URL}/api/v3/series/{series_id}", headers=headers)
        
        if not response.ok:
            return jsonify({'status': 'error', 'message': 'Failed to get series info'}), 500
            
        seasons = response.json().get('seasons', [])
        
        # Filter out seasons with no episodes
        valid_seasons = [s for s in seasons if s.get('statistics', {}).get('totalEpisodeCount', 0) > 0]
        
        return jsonify({'status': 'success', 'seasons': valid_seasons})
    except Exception as e:
        app.logger.error(f"Error getting seasons: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/get-episodes', methods=['GET'])
def get_episodes():
    """Get episodes for a series and season."""
    try:
        series_id = request.args.get('series_id')
        season_number = request.args.get('season')
        
        if not series_id or not season_number:
            return jsonify({'status': 'error', 'message': 'Series ID and season number required'}), 400
            
        import episeerr_utils
        episodes = episeerr_utils.get_series_episodes(int(series_id), int(season_number))
        
        if not episodes:
            return jsonify({'status': 'error', 'message': 'No episodes found'}), 404
            
        # Sort episodes by episode number
        episodes.sort(key=lambda ep: ep.get('episodeNumber', 0))
        
        return jsonify({'status': 'success', 'episodes': episodes})
    except Exception as e:
        app.logger.error(f"Error getting episodes: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/process-episodes', methods=['POST'])
def process_episodes():
    """Process selected episodes."""
    try:
        data = request.json
        series_id = data.get('series_id')
        season_number = data.get('season')
        episode_numbers = data.get('episodes', [])
        
        if not series_id or not season_number or not episode_numbers:
            return jsonify({'status': 'error', 'message': 'Missing required parameters'}), 400
            
        # Convert to integers
        series_id = int(series_id)
        season_number = int(season_number)
        episode_numbers = [int(ep) for ep in episode_numbers]
        
        import episeerr_utils
        
        # Get episodes
        episodes = episeerr_utils.get_series_episodes(series_id, season_number)
        
        # Check if episodes exist
        if not episodes:
            return jsonify({'status': 'error', 'message': 'No episodes found for this season'}), 404
            
        # Monitor selected episodes
        monitor_success = episeerr_utils.monitor_specific_episodes(
            series_id, 
            season_number, 
            episode_numbers
        )
        
        if not monitor_success:
            return jsonify({'status': 'error', 'message': 'Failed to monitor episodes'}), 500
            
        # Get episode IDs for searching
        episode_ids = [
            ep['id'] for ep in episodes 
            if ep['episodeNumber'] in episode_numbers
        ]
        
        # Trigger search for the episodes
        search_success = episeerr_utils.search_episodes(series_id, episode_ids)
        
        if search_success:
            return jsonify({
                'status': 'success', 
                'message': f'Successfully processed {len(episode_numbers)} episodes',
                'episodes': sorted(episode_numbers)
            }), 200
        else:
            return jsonify({'status': 'error', 'message': 'Failed to search for episodes'}), 500
            
    except Exception as e:
        app.logger.error(f"Error processing episodes: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
@app.route('/search-series')
def search_series():
    """Search for series by name."""
    try:
        query = request.args.get('query')
        if not query:
            return jsonify({'status': 'error', 'message': 'No query provided'}), 400
            
        headers = {'X-Api-Key': SONARR_API_KEY}
        
        # Get all series
        response = requests.get(f"{SONARR_URL}/api/v3/series", headers=headers)
        
        if not response.ok:
            return jsonify({'status': 'error', 'message': 'Failed to get series list'}), 500
            
        series_list = response.json()
        
        # Search for matching series
        query = query.lower()
        matching_series = []
        
        # First try exact match
        exact_matches = [s for s in series_list if s['title'].lower() == query]
        if exact_matches:
            matching_series.extend(exact_matches)
        else:
            # Then try contains
            contains_matches = [s for s in series_list if query in s['title'].lower()]
            matching_series.extend(contains_matches)
        
        # Prepare results
        results = []
        for series in matching_series:
            results.append({
                'id': series['id'],
                'title': series['title'],
                'tvdbId': series.get('tvdbId')
            })
            
        return jsonify({'status': 'success', 'series': results})
        
    except Exception as e:
        app.logger.error(f"Error searching series: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
@app.route('/seerr-webhook', methods=['POST'])
def handle_seerr_webhook():
    """Handle incoming webhooks from Jellyseerr/Overseerr."""
    app.logger.info("Received webhook request from Jellyseerr/Overseerr")
    
    try:
        payload = request.json
        app.logger.debug(f"Received payload: {json.dumps(payload, indent=2)}")

        # Extract requested season
        requested_season = None
        for extra in payload.get('extra', []):
            if extra.get('name') == 'Requested Seasons':
                requested_season = int(extra.get('value'))
                break
        
        # Process approved TV content
        if ('APPROVED' in payload.get('notification_type', '').upper() and 
            payload.get('media', {}).get('media_type') == 'tv'):
            
            tvdb_id = payload.get('media', {}).get('tvdbId')
            if not tvdb_id:
                return jsonify({"error": "No TVDB ID"}), 400
                
            # Verify season number
            if not requested_season:
                app.logger.error("Missing required season number")
                return jsonify({"error": "No season specified."}), 400
            
            # Extract request ID
            request_id = payload.get('request', {}).get('id') or payload.get('request', {}).get('request_id')
            
            # Call the process_series function from episeerr_utils
            import episeerr_utils
            success = episeerr_utils.process_series(tvdb_id, requested_season, request_id)
            
            # Always attempt to delete the request
            if request_id:
                try:
                    delete_success = episeerr_utils.delete_overseerr_request(request_id)
                    if not delete_success:
                        app.logger.error(f"Failed to delete request {request_id}")
                except Exception as delete_error:
                    app.logger.error(f"Exception during request deletion: {str(delete_error)}")
            
            response = {
                "status": "success" if success else "failed",
                "message": "Set up series" if success else "Failed to process series"
            }
            return jsonify(response), 200 if success else 500
            
        app.logger.info("Event ignored - not an approved TV request")
        return jsonify({"message": "Ignored event"}), 200
        
    except Exception as e:
        app.logger.error(f"Webhook processing error: {str(e)}", exc_info=True)
        return jsonify({"error": "Processing failed"}), 500

@app.route('/sonarr-webhook', methods=['POST'])
def handle_sonarr_webhook():
    """Handle webhooks from Sonarr for series additions."""
    data = request.json
    if data and data.get('eventType') == 'SeriesAdd':
        try:
            series_id = data.get('series', {}).get('id')
            if series_id:
                from servertosonarr import has_tag, apply_default_rule_to_new_series
                
                # Check if series has the 'episodes' tag
                if has_tag(series_id, "episodes"):
                    app.logger.info(f"Series {series_id} has 'episodes' tag, assigning 'none' rule")
                    
                    # Load the config
                    config = load_config()
                    
                    # Convert to string for consistency
                    series_id_str = str(series_id)
                    
                    # Remove this series from any existing rules
                    for rule_name, rule_details in config['rules'].items():
                        if 'series' in rule_details:
                            rule_details['series'] = [sid for sid in rule_details['series'] if sid != series_id_str]
                    
                    # Add to the "none" rule
                    if "none" not in config['rules']:
                        config['rules']["none"] = {
                            'get_option': "0",
                            'action_option': "monitor",
                            'keep_watched': "0",
                            'monitor_watched': False,
                            'series': []
                        }
                    
                    if series_id_str not in config['rules']["none"]['series']:
                        config['rules']["none"]['series'].append(series_id_str)
                        
                    save_config(config)
                    app.logger.info(f"Assigned series {series_id} to 'none' rule")
                else:
                    # Apply the default 1n1 rule
                    apply_default_rule_to_new_series(series_id)
                    app.logger.info(f"Applied default 1n1 rule to series {series_id}")
                    
                return jsonify({'status': 'success', 'message': 'Processed new series'}), 200
                
        except Exception as e:
            app.logger.error(f"Error processing new series: {str(e)}")
            return jsonify({'status': 'error', 'message': str(e)}), 500
            
    return jsonify({'status': 'success'}), 200



if __name__ == '__main__':
        
    # Start the Flask application
    app.logger.info("Starting webhook listener on port 5001")
    app.run(host='0.0.0.0', port=5001, debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true')