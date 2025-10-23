# %%
import os
import base64
import logging
import requests
import dash, requests
from dash import dcc
from dash import html, dash_table
from dash.dependencies import Output, State, Input
import pandas as pd
import numpy as np
import plotly.express as px
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from cache_manager import FitbitCache


# %%

log = logging.getLogger(__name__)

# Initialize cache
print("🗄️ Initializing Fitbit data cache...")
cache = FitbitCache()

def populate_sleep_score_cache(dates_to_fetch: list, headers: dict):
    """
    Fetch actual sleep scores from Fitbit API for missing dates and cache them.
    This uses the daily endpoint which includes the real sleep score.
    """
    fetched_count = 0
    for date_str in dates_to_fetch:
        try:
            # Fetch individual day's sleep data (includes sleepScore)
            response = requests.get(
                f"https://api.fitbit.com/1.2/user/-/sleep/date/{date_str}.json",
                headers=headers,
                timeout=10
            ).json()
            
            if 'sleep' in response and len(response['sleep']) > 0:
                for sleep_record in response['sleep']:
                    if sleep_record.get('isMainSleep', True):
                        # Extract sleep score
                        sleep_score = None
                        if 'sleepScore' in sleep_record and isinstance(sleep_record['sleepScore'], dict):
                            sleep_score = sleep_record['sleepScore'].get('overall')
                        
                        # Fallback to efficiency if no sleep score
                        if sleep_score is None and 'efficiency' in sleep_record:
                            sleep_score = sleep_record['efficiency']
                        
                        if sleep_score is not None:
                            # Cache the sleep score and related data
                            cache.set_sleep_score(
                                date=date_str,
                                sleep_score=sleep_score,
                                efficiency=sleep_record.get('efficiency'),
                                total_sleep=sleep_record.get('minutesAsleep'),
                                deep=sleep_record.get('levels', {}).get('summary', {}).get('deep', {}).get('minutes'),
                                light=sleep_record.get('levels', {}).get('summary', {}).get('light', {}).get('minutes'),
                                rem=sleep_record.get('levels', {}).get('summary', {}).get('rem', {}).get('minutes'),
                                wake=sleep_record.get('levels', {}).get('summary', {}).get('wake', {}).get('minutes'),
                                start_time=sleep_record.get('startTime'),
                                sleep_data_json=str(sleep_record)
                            )
                            fetched_count += 1
                            print(f"✅ Cached sleep score for {date_str}: {sleep_score}")
                        break  # Only process main sleep
        except Exception as e:
            print(f"⚠️ Error fetching sleep score for {date_str}: {e}")
            continue
    
    return fetched_count

for variable in ['CLIENT_ID','CLIENT_SECRET','REDIRECT_URL'] :
    if variable not in os.environ.keys() :
        log.error(f'Missing required environment variable \'{variable}\', please review the README')
        exit(1)

app = dash.Dash(__name__)
app.title = "Fitbit Wellness Report"
server = app.server

def refresh_access_token(refresh_token):
    """Refresh the access token using the refresh token"""
    try:
        client_id = os.environ['CLIENT_ID']
        client_secret = os.environ['CLIENT_SECRET']
        token_url = 'https://api.fitbit.com/oauth2/token?'
        payload = {'grant_type': 'refresh_token', 'refresh_token': refresh_token}
        token_creds = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
        token_headers = {"Authorization": f"Basic {token_creds}"}
        token_response = requests.post(token_url, data=payload, headers=token_headers)
        token_response_json = token_response.json()
        
        new_access_token = token_response_json.get('access_token')
        new_refresh_token = token_response_json.get('refresh_token')
        expires_in = token_response_json.get('expires_in', 28800)
        
        if new_access_token:
            expiry_time = (datetime.now() + timedelta(seconds=expires_in)).timestamp()
            print("Token refreshed successfully!")
            return new_access_token, new_refresh_token, expiry_time
        else:
            print("Failed to refresh token")
            return None, None, None
    except Exception as e:
        print(f"Error refreshing token: {e}")
        return None, None, None

app.layout = html.Div(children=[
    dcc.ConfirmDialog(
        id='errordialog',
        message='Invalid Access Token : Unable to fetch data',
    ),
    dcc.ConfirmDialog(
        id='rate-limit-dialog',
        message='⚠️ Fitbit API Rate Limit Exceeded!\n\nYou have made too many API requests (150/hour limit).\n\nPlease wait at least 1 hour before generating another report.\n\nTip: Generate shorter date ranges to reduce API calls.',
    ),
    html.Div(id="input-area", className="hidden-print",
    style={
        'display': 'flex',
        'align-items': 'center',
        'justify-content': 'center',
        'gap': '20px',
        'margin': 'auto',
        'flex-wrap': 'wrap',
        'margin-top': '30px'
    },children=[
        dcc.DatePickerRange(
        id='my-date-picker-range',
        display_format='MMMM DD, Y',
        minimum_nights=40,
        max_date_allowed=datetime.today().date() - timedelta(days=1),
        min_date_allowed=datetime.today().date() - timedelta(days=1000),
        end_date=datetime.today().date() - timedelta(days=1),
        start_date=datetime.today().date() - timedelta(days=365)
        ),
        html.Div(style={'display': 'flex', 'flex-direction': 'column', 'align-items': 'flex-start', 'gap': '5px'}, children=[
            dcc.Checklist(
                id='advanced-metrics-toggle',
                options=[{'label': ' Include Advanced Metrics (HRV, Breathing Rate, Temperature)', 'value': 'advanced'}],
                value=[],  # Default: OFF
                style={'font-size': '14px'}
            ),
            html.Div(id='advanced-metrics-warning', style={'font-size': '11px', 'color': '#ff6b6b', 'max-width': '400px', 'display': 'none'}, 
                     children="⚠️ Advanced metrics require many API calls. Use shorter date ranges (30 days) to avoid rate limits (150 requests/hour).")
        ]),
        html.Button(id='submit-button', type='submit', children='Submit', n_clicks=0, className="button-primary"),
        html.Button("Login to FitBit", id="login-button"),
    ]),
    dcc.Location(id="location"),
    dcc.Store(id="oauth-token", storage_type='session'),  # Store OAuth token in session storage
    dcc.Store(id="refresh-token", storage_type='session'),  # Store refresh token in session storage
    dcc.Store(id="token-expiry", storage_type='session'),  # Store token expiry time
    html.Div(id="instruction-area", className="hidden-print", style={'margin-top':'30px', 'margin-right':'auto', 'margin-left':'auto','text-align':'center'}, children=[
        html.P( "Select a date range to generate a report.", style={'font-size':'17px', 'font-weight': 'bold', 'color':'#54565e'}),
        ]),
    html.Div(id='loading-div', style={'margin-top': '40px'}, children=[
    dcc.Loading(
            id="loading-progress",
            type="default",
            children=html.Div(id="loading-output-1")
        ),
    ]),

    html.Div(id='output_div', style={'max-width': '1400px', 'margin': 'auto'}, children=[

        html.Div(id='report-title-div', 
        style={
        'display': 'flex',
        'align-items': 'center',
        'justify-content': 'center',
        'flex-direction': 'column',
        'margin-top': '20px'}, children=[
            html.H2(id="report-title", style={'font-weight': 'bold'}),
            html.H4(id="date-range-title", style={'font-weight': 'bold'}),
            html.P(id="generated-on-title", style={'font-weight': 'bold', 'font-size': '16'})
        ]),
        html.Div(style={"height": '40px'}),
        html.H4("Resting Heart Rate 💖", style={'font-weight': 'bold'}),
        html.H6("Resting heart rate (RHR) is derived from a person's average sleeping heart rate. Fitbit tracks heart rate with photoplethysmography. This technique uses sensors and green light to detect blood volume when the heart beats. If a Fitbit device isn't worn during sleep, RHR is derived from daytime sedentary heart rate. According to the American Heart Association, a normal RHR is between 60-100 beats per minute (bpm), but this can vary based upon your age or fitness level."),
        dcc.Graph(
            id='graph_RHR',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='RHR_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Steps Count 👣", style={'font-weight': 'bold'}),
        html.H6("Fitbit devices use an accelerometer to track steps. Some devices track active minutes, which includes activities over 3 metabolic equivalents (METs), such as brisk walking and cardio workouts."),
        dcc.Graph(
            id='graph_steps',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        dcc.Graph(
            id='graph_steps_heatmap',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='steps_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Activity 🏃‍♂️", style={'font-weight': 'bold'}),
        html.H6("Heart Rate Zones (fat burn, cardio and peak) are based on a percentage of maximum heart rate. Maximum heart rate is calculated as 220 minus age. The Centers for Disease Control recommends that adults do at least 150-300 minutes of moderate-intensity aerobic activity each week or 75-150 minutes of vigorous-intensity aerobic activity each week."),
        dcc.Graph(
            id='graph_activity_minutes',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='fat_burn_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(id='cardio_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(id='peak_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Weight Log ⏲️", style={'font-weight': 'bold'}),
        html.H6("Fitbit connects with the Aria family of smart scales to track weight. Weight may also be self-reported using the Fitbit app. Studies suggest that regular weigh-ins may help people who want to lose weight."),
        dcc.Graph(
            id='graph_weight',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='weight_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("SpO2 🩸", style={'font-weight': 'bold'}),
        html.H6("A pulse oximeter reading indicates what percentage of your blood is saturated, known as the SpO2 level. A typical, healthy reading is 95–100% . If your SpO2 level is less than 92%, a doctor may recommend you get an ABG. A pulse ox is the most common type of test because it's noninvasive and provides quick readings."),
        dcc.Graph(
            id='graph_spo2',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='spo2_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Sleep 💤", style={'font-weight': 'bold'}),
        html.H6("Fitbit estimates sleep stages (awake, REM, light sleep and deep sleep) and sleep duration based on a person's movement and heart-rate patterns. The National Sleep Foundation recommends 7-9 hours of sleep per night for adults"),
        dcc.Checklist(options=[{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled':True}], value=['Color Code Sleep Stages'], style={'max-width': '1330px', 'margin': 'auto'}, inline=True, id="sleep-stage-checkbox", className="hidden-print"),
        dcc.Graph(
            id='graph_sleep',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        dcc.Graph(
            id='graph_sleep_regularity',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='sleep_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Heart Rate Variability (HRV) 💗", style={'font-weight': 'bold'}),
        html.H6("Heart Rate Variability measures the variation in time between heartbeats. Higher HRV generally indicates better cardiovascular fitness and stress resilience. HRV is measured in milliseconds (ms) and varies by age, fitness level, and individual factors."),
        dcc.Graph(
            id='graph_hrv',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='hrv_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Breathing Rate 🫁", style={'font-weight': 'bold'}),
        html.H6("Breathing rate is the number of breaths per minute during sleep. A normal breathing rate for adults is typically between 12-20 breaths per minute. Fitbit calculates this using movement and heart rate sensors during sleep."),
        dcc.Graph(
            id='graph_breathing',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='breathing_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Cardio Fitness Score (VO2 Max) 🏃", style={'font-weight': 'bold'}),
        html.H6("Cardio Fitness Score estimates your VO2 Max - the maximum amount of oxygen your body can use during exercise. Higher scores indicate better cardiovascular fitness. Scores are personalized based on your age, sex, and fitness data."),
        dcc.Graph(
            id='graph_cardio_fitness',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='cardio_fitness_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Temperature 🌡️", style={'font-weight': 'bold'}),
        html.H6("Skin temperature variation from your personal baseline. Temperature changes can indicate illness, stress, or menstrual cycle changes. Measured in degrees relative to your baseline (available on supported devices like Fitbit Sense, Versa 3, Charge 5)."),
        dcc.Graph(
            id='graph_temperature',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='temperature_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Active Zone Minutes ⚡", style={'font-weight': 'bold'}),
        html.H6("Active Zone Minutes track time spent in fat burn, cardio, or peak heart rate zones. The American Heart Association recommends at least 150 Active Zone Minutes per week for health benefits."),
        dcc.Graph(
            id='graph_azm',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='azm_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Calories & Distance 🔥", style={'font-weight': 'bold'}),
        html.H6("Calories burned includes your basal metabolic rate (BMR) plus calories from activity. Distance is calculated from steps and stride length. These metrics help track daily energy expenditure."),
        dcc.Graph(
            id='graph_calories',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        dcc.Graph(
            id='graph_distance',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='calories_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Floors Climbed 🪜", style={'font-weight': 'bold'}),
        html.H6("Floors climbed are calculated using an altimeter that detects elevation changes. One floor is approximately 10 feet (3 meters) of elevation gain."),
        dcc.Graph(
            id='graph_floors',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='floors_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Exercise Log 🏋️", style={'font-weight': 'bold'}),
        html.H6("Logged exercises and workouts tracked by your Fitbit device. Includes activity type, duration, calories burned, and average heart rate for each session."),
        html.Div(style={'display': 'flex', 'gap': '20px', 'align-items': 'center', 'justify-content': 'center', 'margin': '20px'}, children=[
            html.Label("Filter by Activity Type:", style={'font-weight': 'bold'}),
            dcc.Dropdown(
                id='exercise-type-filter',
                options=[],  # Will be populated dynamically
                value='All',
                style={'min-width': '200px'}
            ),
        ]),
        html.Div(id='exercise_log_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '20px'}),
        html.H5("📊 Workout Details", style={'font-weight': 'bold', 'margin-top': '20px'}),
        html.P("Select a date to view detailed heart rate zones for that workout:", style={'color': '#666'}),
        html.Div(style={'display': 'flex', 'gap': '20px', 'align-items': 'center', 'margin': '15px 0'}, children=[
            dcc.Dropdown(
                id='workout-date-selector',
                options=[],
                placeholder="Select a workout date...",
                style={'min-width': '250px'}
            ),
        ]),
        html.Div(id='workout-detail-display', style={'margin': '20px 0'}, children=[]),
        html.Div(style={"height": '40px'}),
        
        html.H4("Sleep Quality Analysis 😴", style={'font-weight': 'bold'}),
        html.H6("Comprehensive sleep metrics including sleep score, stage distribution, and consistency patterns."),
        html.Div(style={'display': 'flex', 'flex-wrap': 'wrap', 'gap': '20px', 'justify-content': 'center'}, children=[
            html.Div(style={'flex': '1', 'min-width': '400px'}, children=[
                dcc.Graph(id='graph_sleep_score', figure=px.line(), config={'displaylogo': False}),
            ]),
            html.Div(style={'flex': '1', 'min-width': '400px'}, children=[
                dcc.Graph(id='graph_sleep_stages_pie', figure=px.pie(), config={'displaylogo': False}),
            ]),
        ]),
        html.Div(style={"height": '20px'}),
        html.H5("📊 Sleep Night Details", style={'font-weight': 'bold', 'margin-top': '20px'}),
        html.P("Select a date to view detailed sleep stages and timeline for that night:", style={'color': '#666'}),
        html.Div(style={'display': 'flex', 'gap': '20px', 'align-items': 'center', 'margin': '15px 0'}, children=[
            dcc.Dropdown(
                id='sleep-date-selector',
                options=[],
                placeholder="Select a sleep date...",
                style={'min-width': '250px'}
            ),
        ]),
        html.Div(id='sleep-detail-display', style={'margin': '20px 0'}, children=[]),
        html.Div(style={"height": '40px'}),
        
        html.H4("Exercise ↔ Sleep Correlations 🔗", style={'font-weight': 'bold'}),
        html.H6("Discover how your workouts impact your sleep quality and next-day recovery."),
        dcc.Graph(id='graph_exercise_sleep_correlation', figure=px.scatter(), config={'displaylogo': False}),
        html.Div(id='correlation_insights', style={'max-width': '1200px', 'margin': 'auto', 'padding': '20px', 'background-color': '#f8f9fa', 'border-radius': '10px'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.Div(style={"height": '25px'}),
    ]),
])

@app.callback(Output('location', 'href'),Input('login-button', 'n_clicks'))
def authorize(n_clicks):
    """Authorize the application"""
    if n_clicks :
        client_id = os.environ['CLIENT_ID']
        redirect_uri = os.environ['REDIRECT_URL']
        scope = 'profile activity cardio_fitness heartrate sleep weight oxygen_saturation respiratory_rate temperature location'
        auth_url = f'https://www.fitbit.com/oauth2/authorize?scope={scope}&client_id={client_id}&response_type=code&prompt=none&redirect_uri={redirect_uri}'
        return auth_url
    return dash.no_update

@app.callback(Output('oauth-token', 'data'),Output('refresh-token', 'data'),Output('token-expiry', 'data'),Input('location', 'href'))
def handle_oauth_callback(href):
    """Process the OAuth callback"""
    if href:
        # Parse the query string from the URL to extract the 'code' parameter
        parsed_url = urlparse(href)
        query_params = parse_qs(parsed_url.query)
        oauth_code = query_params.get('code', [None])[0]
        if oauth_code :
            print(f"OAuth code received: {oauth_code[:20]}...")
        else :
            print("No OAuth code found in URL.")
            return dash.no_update, dash.no_update, dash.no_update
        # Exchange code for a token
        client_id = os.environ['CLIENT_ID']
        client_secret = os.environ['CLIENT_SECRET']
        redirect_uri = os.environ['REDIRECT_URL']
        token_url='https://api.fitbit.com/oauth2/token'
        payload = {
            'code': oauth_code, 
            'grant_type': 'authorization_code', 
            'client_id': client_id, 
            'redirect_uri': redirect_uri
        }
        token_creds = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
        token_headers = {
            "Authorization": f"Basic {token_creds}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        print(f"Requesting token with redirect_uri: {redirect_uri}")
        token_response = requests.post(token_url, data=payload, headers=token_headers)
        print(f"Token response status: {token_response.status_code}")
        print(f"Token response: {token_response.text}")
        
        try:
        token_response_json = token_response.json()
        except:
            print(f"ERROR: Could not parse token response as JSON")
            return dash.no_update, dash.no_update, dash.no_update
            
        access_token = token_response_json.get('access_token')
        refresh_token = token_response_json.get('refresh_token')
        expires_in = token_response_json.get('expires_in', 28800)  # Default 8 hours
        
        if access_token :
            print(f"✅ Access token received! Expires in {expires_in} seconds")
            # Calculate expiry timestamp
            expiry_time = (datetime.now() + timedelta(seconds=expires_in)).timestamp()
            return access_token, refresh_token, expiry_time
        else :
            errors = token_response_json.get('errors', token_response_json.get('error', 'Unknown error'))
            print(f"❌ No access token found in response. Errors: {errors}")
    return dash.no_update, dash.no_update, dash.no_update

@app.callback(Output('login-button', 'children'),Output('login-button', 'disabled'),Input('oauth-token', 'data'))
def update_login_button(oauth_token):
    if oauth_token:
        return html.Span("Logged in"), True
    else:
        return "Login to FitBit", False

@app.callback(Output('advanced-metrics-warning', 'style'), Input('advanced-metrics-toggle', 'value'))
def toggle_advanced_metrics_warning(value):
    if 'advanced' in value:
        return {'font-size': '11px', 'color': '#ff6b6b', 'max-width': '400px', 'display': 'block'}
    return {'font-size': '11px', 'color': '#ff6b6b', 'max-width': '400px', 'display': 'none'}

# Store for exercise data
exercise_data_store = {}

@app.callback(
    Output('exercise_log_table', 'children', allow_duplicate=True),
    Input('exercise-type-filter', 'value'),
    State('exercise_log_table', 'children'),
    prevent_initial_call=True
)
def filter_exercise_log(selected_type, current_table):
    """Filter exercise log by activity type"""
    if not selected_type or not isinstance(current_table, dash_table.DataTable):
        return dash.no_update
    
    # Get the full data from the table
    try:
        full_data = current_table.data
        
        # Filter based on selected type
        if selected_type == 'All':
            filtered_data = full_data
        else:
            filtered_data = [row for row in full_data if row.get('Activity') == selected_type]
        
        if filtered_data and len(filtered_data) > 0:
            return dash_table.DataTable(
                filtered_data,
                [{"name": i, "id": i} for i in full_data[0].keys()],
                style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}],
                style_header={'backgroundColor': '#336699','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'},
                style_cell={'textAlign': 'center'},
                page_size=20
            )
        else:
            return html.P(f"No {selected_type} activities in this period.", style={'text-align': 'center', 'color': '#888'})
    except Exception as e:
        print(f"Error filtering exercise log: {e}")
        return dash.no_update


def seconds_to_tick_label(seconds):
    """Calculate the number of hours, minutes, and remaining seconds"""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    mult, remainder = divmod(hours, 12)
    if mult >=2:
        hours = hours - (12*mult)
    result_datetime = datetime(1, 1, 1, hour=hours, minute=minutes, second=seconds)
    if result_datetime.hour >= 12:
        result_datetime = result_datetime - timedelta(hours=12)
    else:
        result_datetime = result_datetime + timedelta(hours=12)
    return result_datetime.strftime("%H:%M")

def format_minutes(minutes):
    return "%2dh %02dm" % (divmod(minutes, 60))

def calculate_table_data(df, measurement_name):
    df = df.sort_values(by='Date', ascending=False)
    result_data = {
        'Period' : ['30 days', '3 months', '6 months', '1 year'],
        'Average ' + measurement_name : [],
        'Max ' + measurement_name : [],
        'Min ' + measurement_name : []
    }
    last_date = df.head(1)['Date'].values[0]
    for period in [30, 90, 180, 365]:
        end_date = last_date
        start_date = end_date - pd.Timedelta(days=period)
        
        period_data = df[(df['Date'] >= start_date) & (df['Date'] <= end_date)]
        
        if len(period_data) >= period:

            max_hr = period_data[measurement_name].max()
            if measurement_name == "Steps Count":
                min_hr = period_data[period_data[measurement_name] != 0][measurement_name].min()
            else:
                min_hr = period_data[measurement_name].min()
            average_hr = round(period_data[measurement_name].mean(),2)
            
            if measurement_name == "Total Sleep Minutes":
                result_data['Average ' + measurement_name].append(format_minutes(average_hr))
                result_data['Max ' + measurement_name].append(format_minutes(max_hr))
                result_data['Min ' + measurement_name].append(format_minutes(min_hr))
            else:
                result_data['Average ' + measurement_name].append(average_hr)
                result_data['Max ' + measurement_name].append(max_hr)
                result_data['Min ' + measurement_name].append(min_hr)
        else:
            result_data['Average ' + measurement_name].append(pd.NA)
            result_data['Max ' + measurement_name].append(pd.NA)
            result_data['Min ' + measurement_name].append(pd.NA)
    
    return pd.DataFrame(result_data)

# Sleep stages checkbox functionality
@app.callback(Output('graph_sleep', 'figure', allow_duplicate=True), Input('sleep-stage-checkbox', 'value'), State('graph_sleep', 'figure'), prevent_initial_call=True)
def update_sleep_colors(value, fig):
    if len(value) == 1:
        fig['data'][0]['marker']['color'] = '#084466'
        fig['data'][1]['marker']['color'] = '#1e9ad6'
        fig['data'][2]['marker']['color'] = '#4cc5da'
        fig['data'][3]['marker']['color'] = '#fd7676'
    else:
        fig['data'][0]['marker']['color'] = '#084466'
        fig['data'][1]['marker']['color'] = '#084466'
        fig['data'][2]['marker']['color'] = '#084466'
        fig['data'][3]['marker']['color'] = '#084466'
    return fig

# Limits the date range to one year max
@app.callback(Output('my-date-picker-range', 'max_date_allowed'), Output('my-date-picker-range', 'end_date'),
             [Input('my-date-picker-range', 'start_date')])
def set_max_date_allowed(start_date):
    start = datetime.strptime(start_date, "%Y-%m-%d")
    current_date = datetime.today().date() - timedelta(days=1)
    max_end_date = min((start + timedelta(days=365)).date(), current_date)
    return max_end_date, max_end_date

# Disables the button after click and starts calculations
@app.callback(Output('errordialog', 'displayed'), Output('submit-button', 'disabled'), Output('my-date-picker-range', 'disabled'), Input('submit-button', 'n_clicks'),State('oauth-token', 'data'),State('refresh-token', 'data'),State('token-expiry', 'data'),prevent_initial_call=True)
def disable_button_and_calculate(n_clicks, oauth_token, refresh_token, token_expiry):
    print(f"🔍 Submit button clicked. Token present: {oauth_token is not None}")
    print(f"🔍 Refresh token present: {refresh_token is not None}")
    print(f"🔍 Token expiry: {token_expiry}")
    
    if not oauth_token:
        print("❌ No OAuth token found!")
        return True, False, False
    
    # Try to refresh token if it's close to expiring
    if refresh_token and token_expiry:
        current_time = datetime.now().timestamp()
        print(f"🔍 Current time: {current_time}, Expiry: {token_expiry}, Diff: {token_expiry - current_time} seconds")
        if current_time >= (token_expiry - 1800):  # Less than 30 min left
            print("⏱️ Token expiring soon, refreshing before data fetch...")
            new_token, new_refresh, new_expiry = refresh_access_token(refresh_token)
            if new_token:
                oauth_token = new_token
                print("✅ Token refreshed successfully!")
            else:
                print("❌ Token refresh failed!")
    
    headers = {
        "Authorization": "Bearer " + oauth_token,
        "Accept": "application/json"
    }
    try:
        print("🔍 Validating token with profile API...")
        token_response = requests.get("https://api.fitbit.com/1/user/-/profile.json", headers=headers, timeout=10)
        print(f"🔍 Validation response status: {token_response.status_code}")
        if token_response.status_code != 200:
            print(f"❌ Validation failed! Response: {token_response.text[:200]}")
        token_response.raise_for_status()
        print("✅ Token validation successful!")
    except Exception as e:
        print(f"❌ Token validation exception: {type(e).__name__}: {str(e)}")
        return True, False, False
    return False, True, True

# Fetch data and update graphs on click of submit
@app.callback(Output('report-title', 'children'), Output('date-range-title', 'children'), Output('generated-on-title', 'children'), Output('graph_RHR', 'figure'), Output('RHR_table', 'children'), Output('graph_steps', 'figure'), Output('graph_steps_heatmap', 'figure'), Output('steps_table', 'children'), Output('graph_activity_minutes', 'figure'), Output('fat_burn_table', 'children'), Output('cardio_table', 'children'), Output('peak_table', 'children'), Output('graph_weight', 'figure'), Output('weight_table', 'children'), Output('graph_spo2', 'figure'), Output('spo2_table', 'children'), Output('graph_sleep', 'figure'), Output('graph_sleep_regularity', 'figure'), Output('sleep_table', 'children'), Output('sleep-stage-checkbox', 'options'), Output('graph_hrv', 'figure'), Output('hrv_table', 'children'), Output('graph_breathing', 'figure'), Output('breathing_table', 'children'), Output('graph_cardio_fitness', 'figure'), Output('cardio_fitness_table', 'children'), Output('graph_temperature', 'figure'), Output('temperature_table', 'children'), Output('graph_azm', 'figure'), Output('azm_table', 'children'), Output('graph_calories', 'figure'), Output('graph_distance', 'figure'), Output('calories_table', 'children'), Output('graph_floors', 'figure'), Output('floors_table', 'children'), Output('exercise-type-filter', 'options'), Output('exercise_log_table', 'children'), Output('workout-date-selector', 'options'), Output('graph_sleep_score', 'figure'), Output('graph_sleep_stages_pie', 'figure'), Output('sleep-date-selector', 'options'), Output('graph_exercise_sleep_correlation', 'figure'), Output('correlation_insights', 'children'), Output("loading-output-1", "children"),
Input('submit-button', 'disabled'),
State('my-date-picker-range', 'start_date'), State('my-date-picker-range', 'end_date'), State('oauth-token', 'data'), State('advanced-metrics-toggle', 'value'),
prevent_initial_call=True)
def update_output(n_clicks, start_date, end_date, oauth_token, advanced_metrics_enabled):

    start_date = datetime.fromisoformat(start_date).strftime("%Y-%m-%d")
    end_date = datetime.fromisoformat(end_date).strftime("%Y-%m-%d")

    headers = {
        "Authorization": "Bearer " + oauth_token,
        "Accept": "application/json"
    }

    # Collecting data-----------------------------------------------------------------------------------------------------------------------
    
    try:
    user_profile = requests.get("https://api.fitbit.com/1/user/-/profile.json", headers=headers).json()
        
        # Check for rate limiting or errors
        if 'error' in user_profile:
            error_code = user_profile['error'].get('code')
            if error_code == 429:
                print("⚠️ RATE LIMIT EXCEEDED! Fitbit API limit: 150 requests/hour")
                print("Please wait at least 1 hour before generating another report.")
                # Return with error message
                empty_fig = px.line(title="Rate Limit Exceeded - Please wait 1 hour")
                empty_heatmap = px.imshow([[0]], title="Rate Limit Exceeded")
                return "⚠️ Rate Limit Exceeded", "Please wait at least 1 hour before trying again", "", empty_fig, [], px.bar(), empty_heatmap, [], px.bar(), [], [], [], px.line(), [], px.scatter(), [], px.bar(), px.bar(), [], [{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled': True}], px.line(), [], px.line(), [], px.line(), [], px.line(), [], px.bar(), [], px.bar(), px.bar(), [], px.bar(), [], [{'label': 'All', 'value': 'All'}], html.P("Rate limit exceeded"), px.line(), px.pie(), px.scatter(), html.P("Rate limit exceeded"), ""
            else:
                print(f"API Error: {user_profile['error']}")
                
    response_heartrate = requests.get("https://api.fitbit.com/1/user/-/activities/heart/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
        
        # Check for rate limiting in heart rate response
        if 'error' in response_heartrate:
            error_code = response_heartrate['error'].get('code')
            if error_code == 429:
                print("⚠️ RATE LIMIT EXCEEDED! Fitbit API limit: 150 requests/hour")
                print("Please wait at least 1 hour before generating another report.")
                empty_fig = px.line(title="Rate Limit Exceeded - Please wait 1 hour")
                empty_heatmap = px.imshow([[0]], title="Rate Limit Exceeded")
                return "⚠️ Rate Limit Exceeded", "Please wait at least 1 hour before trying again", "", empty_fig, [], px.bar(), empty_heatmap, [], px.bar(), [], [], [], px.line(), [], px.scatter(), [], px.bar(), px.bar(), [], [{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled': True}], px.line(), [], px.line(), [], px.line(), [], px.line(), [], px.bar(), [], px.bar(), px.bar(), [], px.bar(), [], [{'label': 'All', 'value': 'All'}], html.P("Rate limit exceeded"), px.line(), px.pie(), px.scatter(), html.P("Rate limit exceeded"), ""
                
    response_steps = requests.get("https://api.fitbit.com/1/user/-/activities/steps/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
    response_weight = requests.get("https://api.fitbit.com/1/user/-/body/weight/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
    response_spo2 = requests.get("https://api.fitbit.com/1/user/-/spo2/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
    except Exception as e:
        print(f"ERROR fetching initial data: {e}")
        # Return empty results if API calls fail with valid empty plots
        empty_fig = px.line(title="Error Fetching Data")
        empty_heatmap = px.imshow([[0]], title="No Data Available")
        return dash.no_update, dash.no_update, dash.no_update, empty_fig, [], px.bar(), empty_heatmap, [], px.bar(), [], [], [], px.line(), [], px.scatter(), [], px.bar(), px.bar(), [], [{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled': True}], px.line(), [], px.line(), [], px.line(), [], px.line(), [], px.bar(), [], px.bar(), px.bar(), [], px.bar(), [], [{'label': 'All', 'value': 'All'}], html.P("Error fetching data"), px.line(), px.pie(), px.scatter(), html.P("Error fetching data"), ""
    
    # Build dates list early for parallel fetching
    temp_dates_list = []
    if 'activities-heart' in response_heartrate:
        for entry in response_heartrate['activities-heart']:
            temp_dates_list.append(entry['dateTime'])
    else:
        print(f"ERROR: No heart rate data in response: {response_heartrate}")
        empty_heatmap = px.imshow([[0]], title="No Data Available")
        return dash.no_update, dash.no_update, dash.no_update, px.line(), [], px.bar(), empty_heatmap, [], px.bar(), [], [], [], px.line(), [], px.scatter(), [], px.bar(), px.bar(), [], [{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled': True}], px.line(), [], px.line(), [], px.line(), [], px.line(), [], px.bar(), [], px.bar(), px.bar(), [], px.bar(), [], [{'label': 'All', 'value': 'All'}], html.P("No heart rate data"), px.line(), px.pie(), px.scatter(), html.P("No heart rate data"), ""
    
    # New data endpoints - Parallel day-by-day fetching (range endpoints confirmed don't work)
    # ONLY fetch if advanced metrics are enabled to avoid rate limiting
    
    response_hrv = {"hrv": []}
    response_breathing = {"br": []}
    response_temperature = {"tempSkin": []}
    
    if advanced_metrics_enabled and 'advanced' in advanced_metrics_enabled:
        print("🔬 Advanced metrics enabled - fetching HRV, Breathing Rate, and Temperature...")
        print(f"⚠️ This will make ~{len(temp_dates_list) * 3} additional API calls")
        
        def fetch_hrv_day(date_str):
            try:
                hrv_day = requests.get(f"https://api.fitbit.com/1/user/-/hrv/date/{date_str}.json", headers=headers, timeout=10).json()
                if "hrv" in hrv_day and len(hrv_day["hrv"]) > 0:
                    return {"dateTime": date_str, "value": hrv_day["hrv"][0]["value"]}
            except:
                pass
            return None
        
        def fetch_breathing_day(date_str):
            try:
                br_day = requests.get(f"https://api.fitbit.com/1/user/-/br/date/{date_str}.json", headers=headers, timeout=10).json()
                if "br" in br_day and len(br_day["br"]) > 0:
                    return {"dateTime": date_str, "value": br_day["br"][0]["value"]}
            except:
                pass
            return None
        
        def fetch_temperature_day(date_str):
            try:
                temp_day = requests.get(f"https://api.fitbit.com/1/user/-/temp/skin/date/{date_str}.json", headers=headers, timeout=10).json()
                if "tempSkin" in temp_day and len(temp_day["tempSkin"]) > 0:
                    return {"dateTime": date_str, "value": temp_day["tempSkin"][0]["value"]}
            except:
                pass
            return None
        
        # Fetch HRV, Breathing, and Temperature in parallel
        with ThreadPoolExecutor(max_workers=20) as executor:
            # Submit all requests for all dates simultaneously
            hrv_futures = {executor.submit(fetch_hrv_day, date): date for date in temp_dates_list}
            br_futures = {executor.submit(fetch_breathing_day, date): date for date in temp_dates_list}
            temp_futures = {executor.submit(fetch_temperature_day, date): date for date in temp_dates_list}
            
            # Collect results
            for future in as_completed(hrv_futures):
                result = future.result()
                if result:
                    response_hrv["hrv"].append(result)
            
            for future in as_completed(br_futures):
                result = future.result()
                if result:
                    response_breathing["br"].append(result)
            
            for future in as_completed(temp_futures):
                result = future.result()
                if result:
                    response_temperature["tempSkin"].append(result)
        
        print(f"HRV: Fetched {len(response_hrv.get('hrv', []))} days")
        print(f"Breathing: Fetched {len(response_breathing.get('br', []))} days")
        print(f"Temperature: Fetched {len(response_temperature.get('tempSkin', []))} days")
    else:
        print("ℹ️ Advanced metrics disabled - skipping HRV, Breathing Rate, and Temperature to conserve API calls")
    
    # Cardio Fitness - Fetch in 30-day chunks (API limitation)
    response_cardio_fitness = {"cardioScore": []}
    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    current_dt = start_dt
    while current_dt <= end_dt:
        chunk_end = min(current_dt + timedelta(days=29), end_dt)
        try:
            cf_chunk = requests.get(f"https://api.fitbit.com/1/user/-/cardioscore/date/{current_dt.strftime('%Y-%m-%d')}/{chunk_end.strftime('%Y-%m-%d')}.json", headers=headers).json()
            if "cardioScore" in cf_chunk:
                response_cardio_fitness["cardioScore"].extend(cf_chunk["cardioScore"])
        except:
            pass
        current_dt = chunk_end + timedelta(days=1)
    print(f"Cardio Fitness API Response: Fetched {len(response_cardio_fitness.get('cardioScore', []))} days of data")
    try:
        response_calories = requests.get("https://api.fitbit.com/1/user/-/activities/calories/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
    except:
        response_calories = {}
    try:
        response_distance = requests.get("https://api.fitbit.com/1/user/-/activities/distance/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
    except:
        response_distance = {}
    try:
        response_floors = requests.get("https://api.fitbit.com/1/user/-/activities/floors/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
    except:
        response_floors = {}
    try:
        response_azm = requests.get("https://api.fitbit.com/1/user/-/activities/active-zone-minutes/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
    except:
        response_azm = {}
    try:
        response_activities = requests.get("https://api.fitbit.com/1/user/-/activities/list.json?afterDate="+ start_date +"&sort=asc&offset=0&limit=100", headers=headers).json()
    except:
        response_activities = {}

    # Processing data-----------------------------------------------------------------------------------------------------------------------
    days_name_list = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday','Sunday')
    report_title = "Wellness Report - " + user_profile["user"]["firstName"] + " " + user_profile["user"]["lastName"]
    report_dates_range = datetime.fromisoformat(start_date).strftime("%d %B, %Y") + " – " + datetime.fromisoformat(end_date).strftime("%d %B, %Y")
    generated_on_date = "Report Generated : " + datetime.today().date().strftime("%d %B, %Y")
    dates_list = []
    dates_str_list = []
    rhr_list = []
    steps_list = []
    weight_list = []
    spo2_list = []
    sleep_record_dict = {}
    deep_sleep_list, light_sleep_list, rem_sleep_list, awake_list, total_sleep_list, sleep_start_times_list = [],[],[],[],[],[]
    fat_burn_minutes_list, cardio_minutes_list, peak_minutes_list = [], [], []
    
    # New data lists
    hrv_list = []
    breathing_list = []
    cardio_fitness_list = []
    temperature_list = []
    calories_list = []
    distance_list = []
    floors_list = []
    azm_list = []

    for entry in response_heartrate['activities-heart']:
        dates_str_list.append(entry['dateTime'])
        dates_list.append(datetime.strptime(entry['dateTime'], '%Y-%m-%d'))
        try:
            fat_burn_minutes_list.append(entry["value"]["heartRateZones"][1]["minutes"])
            cardio_minutes_list.append(entry["value"]["heartRateZones"][2]["minutes"])
            peak_minutes_list.append(entry["value"]["heartRateZones"][3]["minutes"])
        except KeyError as E:
            fat_burn_minutes_list.append(None)
            cardio_minutes_list.append(None)
            peak_minutes_list.append(None)
        if 'restingHeartRate' in entry['value']:
            rhr_list.append(entry['value']['restingHeartRate'])
        else:
            rhr_list.append(None)
    
    for entry in response_steps['activities-steps']:
        if int(entry['value']) == 0:
            steps_list.append(None)
        else:
            steps_list.append(int(entry['value']))

    for entry in response_weight["body-weight"]:
        # Convert kg to lbs (1 kg = 2.20462 lbs)
        weight_kg = float(entry['value'])
        weight_lbs = round(weight_kg * 2.20462, 1)
        weight_list.append(weight_lbs)
    
    for entry in response_spo2:
        spo2_list += [None]*(dates_str_list.index(entry["dateTime"])-len(spo2_list))
        spo2_list.append(entry["value"]["avg"])
    spo2_list += [None]*(len(dates_str_list)-len(spo2_list))
    
    # Process HRV data
    for entry in response_hrv.get("hrv", []):
        try:
            hrv_list += [None]*(dates_str_list.index(entry["dateTime"])-len(hrv_list))
            hrv_list.append(entry["value"]["dailyRmssd"])
        except (KeyError, ValueError):
            pass
    hrv_list += [None]*(len(dates_str_list)-len(hrv_list))
    
    # Process Breathing Rate data
    for entry in response_breathing.get("br", []):
        try:
            breathing_list += [None]*(dates_str_list.index(entry["dateTime"])-len(breathing_list))
            breathing_list.append(entry["value"]["breathingRate"])
        except (KeyError, ValueError):
            pass
    breathing_list += [None]*(len(dates_str_list)-len(breathing_list))
    
    # Process Cardio Fitness Score data
    for entry in response_cardio_fitness.get("cardioScore", []):
        try:
            cardio_fitness_list += [None]*(dates_str_list.index(entry["dateTime"])-len(cardio_fitness_list))
            vo2max_value = entry["value"]["vo2Max"]
            
            # Handle range values (e.g., "42-46") by taking the midpoint
            if isinstance(vo2max_value, str) and '-' in vo2max_value:
                parts = vo2max_value.split('-')
                if len(parts) == 2:
                    try:
                        vo2max_value = (float(parts[0]) + float(parts[1])) / 2
                    except:
                        vo2max_value = float(parts[0])  # Use first value if conversion fails
            
            cardio_fitness_list.append(float(vo2max_value) if vo2max_value else None)
        except (KeyError, ValueError, TypeError):
            pass
    cardio_fitness_list += [None]*(len(dates_str_list)-len(cardio_fitness_list))
    
    # Process Temperature data
    for entry in response_temperature.get("tempSkin", []):
        try:
            temperature_list += [None]*(dates_str_list.index(entry["dateTime"])-len(temperature_list))
            # Temperature value might be nested or direct
            if isinstance(entry["value"], dict):
                temperature_list.append(entry["value"].get("nightlyRelative", entry["value"].get("value")))
            else:
                temperature_list.append(entry["value"])
        except (KeyError, ValueError):
            pass
    temperature_list += [None]*(len(dates_str_list)-len(temperature_list))
    
    # Process Calories data
    for entry in response_calories.get('activities-calories', []):
        try:
            calories_list.append(int(entry['value']))
        except (KeyError, ValueError):
            calories_list.append(None)
    # Ensure same length as dates
    while len(calories_list) < len(dates_str_list):
        calories_list.append(None)
    
    # Process Distance data
    for entry in response_distance.get('activities-distance', []):
        try:
            # Convert km to miles (1 km = 0.621371 miles)
            distance_km = float(entry['value'])
            distance_miles = round(distance_km * 0.621371, 2)
            distance_list.append(distance_miles)
        except (KeyError, ValueError):
            distance_list.append(None)
    # Ensure same length as dates
    while len(distance_list) < len(dates_str_list):
        distance_list.append(None)
    
    # Process Floors data
    for entry in response_floors.get('activities-floors', []):
        try:
            floors_list.append(int(entry['value']))
        except (KeyError, ValueError):
            floors_list.append(None)
    # Ensure same length as dates
    while len(floors_list) < len(dates_str_list):
        floors_list.append(None)
    
    # Process Active Zone Minutes data
    for entry in response_azm.get('activities-active-zone-minutes', []):
        try:
            azm_list.append(entry['value']['activeZoneMinutes'])
        except (KeyError, ValueError):
            azm_list.append(None)
    # Ensure same length as dates
    while len(azm_list) < len(dates_str_list):
        azm_list.append(None)

    for i in range(0,len(dates_str_list),100):
        end_index = i+100
        if i+100 > len(dates_str_list):
            end_index = len(dates_str_list)
        temp_start_date = dates_str_list[i]
        temp_end_date = dates_str_list[end_index-1]

        response_sleep = requests.get("https://api.fitbit.com/1.2/user/-/sleep/date/"+ temp_start_date +"/"+ temp_end_date +".json", headers=headers).json()

        # Check if sleep data exists in response
        if "sleep" not in response_sleep:
            print(f"Sleep API returned unexpected response: {response_sleep}")
            continue

        for sleep_record in response_sleep["sleep"][::-1]:
            if sleep_record['isMainSleep']:
                try:
                    sleep_start_time = datetime.strptime(sleep_record["startTime"], "%Y-%m-%dT%H:%M:%S.%f")
                    if sleep_start_time.hour < 12:
                        sleep_start_time = sleep_start_time + timedelta(hours=12)
                    else:
                        sleep_start_time = sleep_start_time + timedelta(hours=-12)
                    sleep_time_of_day = sleep_start_time.time()
                    # Get the actual sleep score - it's nested in a sleepScore object
                    sleep_score = None
                    if 'sleepScore' in sleep_record and isinstance(sleep_record['sleepScore'], dict):
                        sleep_score = sleep_record['sleepScore'].get('overall', None)
                        print(f"Sleep score for {sleep_record['dateOfSleep']}: {sleep_score} (from sleepScore.overall)")
                    elif 'efficiency' in sleep_record:
                        # Fallback to efficiency if no sleep score available
                        sleep_score = sleep_record['efficiency']
                        print(f"Sleep score for {sleep_record['dateOfSleep']}: {sleep_score} (from efficiency - no sleepScore available)")
                    
                    sleep_record_dict[sleep_record['dateOfSleep']] = {
                        'deep': sleep_record['levels']['summary']['deep']['minutes'],
                                                                    'light': sleep_record['levels']['summary']['light']['minutes'],
                                                                    'rem': sleep_record['levels']['summary']['rem']['minutes'],
                                                                    'wake': sleep_record['levels']['summary']['wake']['minutes'],
                                                                    'total_sleep': sleep_record["minutesAsleep"],
                        'start_time_seconds': (sleep_time_of_day.hour * 3600) + (sleep_time_of_day.minute * 60) + sleep_time_of_day.second,
                        'sleep_score': sleep_score,  # Fitbit's actual sleep score from sleepScore.overall
                        'sleep_record': sleep_record  # Store full record for drill-down
                                                                    }
                except KeyError as E:
                    pass

    for day in dates_str_list:
        if day in sleep_record_dict:
            deep_sleep_list.append(sleep_record_dict[day]['deep'])
            light_sleep_list.append(sleep_record_dict[day]['light'])
            rem_sleep_list.append(sleep_record_dict[day]['rem'])
            awake_list.append(sleep_record_dict[day]['wake'])
            total_sleep_list.append(sleep_record_dict[day]['total_sleep'])
            sleep_start_times_list.append(sleep_record_dict[day]['start_time_seconds'])
        else:
            deep_sleep_list.append(None)
            light_sleep_list.append(None)
            rem_sleep_list.append(None)
            awake_list.append(None)
            total_sleep_list.append(None)
            sleep_start_times_list.append(None)

    df_merged = pd.DataFrame({
    "Date": dates_list,
    "Resting Heart Rate": rhr_list,
    "Steps Count": steps_list,
    "Fat Burn Minutes": fat_burn_minutes_list,
    "Cardio Minutes": cardio_minutes_list,
    "Peak Minutes": peak_minutes_list,
    "weight": weight_list,
    "SPO2": spo2_list,
    "Deep Sleep Minutes": deep_sleep_list,
    "Light Sleep Minutes": light_sleep_list,
    "REM Sleep Minutes": rem_sleep_list,
    "Awake Minutes": awake_list,
    "Total Sleep Minutes": total_sleep_list,
    "Sleep Start Time Seconds": sleep_start_times_list,
    "HRV": hrv_list,
    "Breathing Rate": breathing_list,
    "Cardio Fitness Score": cardio_fitness_list,
    "Temperature": temperature_list,
    "Calories": calories_list,
    "Distance": distance_list,
    "Floors": floors_list,
    "Active Zone Minutes": azm_list
    })
    
    df_merged['Total Sleep Seconds'] = df_merged['Total Sleep Minutes']*60
    df_merged["Sleep End Time Seconds"] = df_merged["Sleep Start Time Seconds"] + df_merged['Total Sleep Seconds']
    df_merged["Total Active Minutes"] = df_merged["Fat Burn Minutes"] + df_merged["Cardio Minutes"] + df_merged["Peak Minutes"]
    rhr_avg = {'overall': round(df_merged["Resting Heart Rate"].mean(),1), '30d': round(df_merged["Resting Heart Rate"].tail(30).mean(),1)}
    steps_avg = {'overall': int(df_merged["Steps Count"].mean()), '30d': int(df_merged["Steps Count"].tail(31).mean())}
    weight_avg = {'overall': round(df_merged["weight"].mean(),1), '30d': round(df_merged["weight"].tail(30).mean(),1)}
    spo2_avg = {'overall': round(df_merged["SPO2"].mean(),1), '30d': round(df_merged["SPO2"].tail(30).mean(),1)}
    sleep_avg = {'overall': round(df_merged["Total Sleep Minutes"].mean(),1), '30d': round(df_merged["Total Sleep Minutes"].tail(30).mean(),1)}
    active_mins_avg = {'overall': round(df_merged["Total Active Minutes"].mean(),2), '30d': round(df_merged["Total Active Minutes"].tail(30).mean(),2)}
    weekly_steps_array = np.array([0]*days_name_list.index(datetime.fromisoformat(start_date).strftime('%A')) + df_merged["Steps Count"].to_list() + [0]*(6 - days_name_list.index(datetime.fromisoformat(end_date).strftime('%A'))))
    weekly_steps_array = np.transpose(weekly_steps_array.reshape((int(len(weekly_steps_array)/7), 7)))
    weekly_steps_array = pd.DataFrame(weekly_steps_array, index=days_name_list)

    # Plotting data-----------------------------------------------------------------------------------------------------------------------

    fig_rhr = px.line(df_merged, x="Date", y="Resting Heart Rate", line_shape="spline", color_discrete_sequence=["#d30f1c"], title=f"<b>Daily Resting Heart Rate<br><br><sup>Overall average : {rhr_avg['overall']} bpm | Last 30d average : {rhr_avg['30d']} bpm</sup></b><br><br><br>")
    if df_merged["Resting Heart Rate"].dtype != object:
        fig_rhr.add_annotation(x=df_merged.iloc[df_merged["Resting Heart Rate"].idxmax()]["Date"], y=df_merged["Resting Heart Rate"].max(), text=str(df_merged["Resting Heart Rate"].max()), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_rhr.add_annotation(x=df_merged.iloc[df_merged["Resting Heart Rate"].idxmin()]["Date"], y=df_merged["Resting Heart Rate"].min(), text=str(df_merged["Resting Heart Rate"].min()), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    fig_rhr.add_hline(y=df_merged["Resting Heart Rate"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["Resting Heart Rate"].mean(), 1)) + " BPM", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    fig_rhr.add_hrect(y0=62, y1=68, fillcolor="green", opacity=0.15, line_width=0)
    rhr_summary_df = calculate_table_data(df_merged, "Resting Heart Rate")
    rhr_summary_table = dash_table.DataTable(rhr_summary_df.to_dict('records'), [{"name": i, "id": i} for i in rhr_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#5f040a','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_steps = px.bar(df_merged, x="Date", y="Steps Count", color_discrete_sequence=["#2fb376"], title=f"<b>Daily Steps Count<br><br><sup>Overall average : {steps_avg['overall']} steps | Last 30d average : {steps_avg['30d']} steps</sup></b><br><br><br>")
    if df_merged["Steps Count"].dtype != object:
        fig_steps.add_annotation(x=df_merged.iloc[df_merged["Steps Count"].idxmax()]["Date"], y=df_merged["Steps Count"].max(), text=str(df_merged["Steps Count"].max())+" steps", showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_steps.add_annotation(x=df_merged.iloc[df_merged["Steps Count"].idxmin()]["Date"], y=df_merged["Steps Count"].min(), text=str(df_merged["Steps Count"].min())+" steps", showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    fig_steps.add_hline(y=df_merged["Steps Count"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["Steps Count"].mean(), 1)) + " Steps", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.8, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    fig_steps_heatmap = px.imshow(weekly_steps_array, color_continuous_scale='YLGn', origin='lower', title="<b>Weekly Steps Heatmap</b>", labels={'x':"Week Number", 'y': "Day of the Week"}, height=350, aspect='equal')
    fig_steps_heatmap.update_traces(colorbar_orientation='h', selector=dict(type='heatmap'))
    steps_summary_df = calculate_table_data(df_merged, "Steps Count")
    steps_summary_table = dash_table.DataTable(steps_summary_df.to_dict('records'), [{"name": i, "id": i} for i in steps_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#072f1c','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_activity_minutes = px.bar(df_merged, x="Date", y=["Fat Burn Minutes", "Cardio Minutes", "Peak Minutes"], title=f"<b>Activity Minutes<br><br><sup>Overall total active minutes average : {active_mins_avg['overall']} minutes | Last 30d total active minutes average : {active_mins_avg['30d']} minutes</sup></b><br><br><br>")
    fig_activity_minutes.update_layout(yaxis_title='Active Minutes', legend=dict(orientation="h",yanchor="bottom", y=1.02, xanchor="right", x=1, title_text=''))
    fat_burn_summary_df = calculate_table_data(df_merged, "Fat Burn Minutes")
    fat_burn_summary_table = dash_table.DataTable(fat_burn_summary_df.to_dict('records'), [{"name": i, "id": i} for i in fat_burn_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#636efa','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    cardio_summary_df = calculate_table_data(df_merged, "Cardio Minutes")
    cardio_summary_table = dash_table.DataTable(cardio_summary_df.to_dict('records'), [{"name": i, "id": i} for i in cardio_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#ef553b','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    peak_summary_df = calculate_table_data(df_merged, "Peak Minutes")
    peak_summary_table = dash_table.DataTable(peak_summary_df.to_dict('records'), [{"name": i, "id": i} for i in peak_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#00cc96','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_weight = px.line(df_merged, x="Date", y="weight", line_shape="spline", color_discrete_sequence=["#6b3908"], title=f"<b>Weight<br><br><sup>Overall average : {weight_avg['overall']} lbs | Last 30d average : {weight_avg['30d']} lbs</sup></b><br><br><br>", labels={"weight": "Weight (lbs)"})
    if df_merged["weight"].dtype != object:
        fig_weight.add_annotation(x=df_merged.iloc[df_merged["weight"].idxmax()]["Date"], y=df_merged["weight"].max(), text=str(df_merged["weight"].max()) + " lbs", showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_weight.add_annotation(x=df_merged.iloc[df_merged["weight"].idxmin()]["Date"], y=df_merged["weight"].min(), text=str(df_merged["weight"].min()) + " lbs", showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    fig_weight.add_hline(y=round(df_merged["weight"].mean(),1), line_dash="dot",annotation_text="Average : " + str(round(df_merged["weight"].mean(), 1)) + " lbs", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    weight_summary_df = calculate_table_data(df_merged, "weight")
    weight_summary_table = dash_table.DataTable(weight_summary_df.to_dict('records'), [{"name": i, "id": i} for i in weight_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#4c3b7d','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_spo2 = px.scatter(df_merged, x="Date", y="SPO2", color_discrete_sequence=["#983faa"], title=f"<b>SPO2 Percentage<br><br><sup>Overall average : {spo2_avg['overall']}% | Last 30d average : {spo2_avg['30d']}% </sup></b><br><br><br>", range_y=(90,100), labels={'SPO2':"SpO2(%)"})
    if df_merged["SPO2"].dtype != object:
        fig_spo2.add_annotation(x=df_merged.iloc[df_merged["SPO2"].idxmax()]["Date"], y=df_merged["SPO2"].max(), text=str(df_merged["SPO2"].max())+"%", showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_spo2.add_annotation(x=df_merged.iloc[df_merged["SPO2"].idxmin()]["Date"], y=df_merged["SPO2"].min(), text=str(df_merged["SPO2"].min())+"%", showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    fig_spo2.add_hline(y=df_merged["SPO2"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["SPO2"].mean(), 1)) + "%", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    fig_spo2.update_traces(marker_size=6)
    spo2_summary_df = calculate_table_data(df_merged, "SPO2")
    spo2_summary_table = dash_table.DataTable(spo2_summary_df.to_dict('records'), [{"name": i, "id": i} for i in spo2_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#8d3a18','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_sleep_minutes = px.bar(df_merged, x="Date", y=["Deep Sleep Minutes", "Light Sleep Minutes", "REM Sleep Minutes", "Awake Minutes"], title=f"<b>Sleep Stages<br><br><sup>Overall average : {format_minutes(int(sleep_avg['overall']))} | Last 30d average : {format_minutes(int(sleep_avg['30d']))}</sup></b><br><br>", color_discrete_map={"Deep Sleep Minutes": '#084466', "Light Sleep Minutes": '#1e9ad6', "REM Sleep Minutes": '#4cc5da', "Awake Minutes": '#fd7676',}, height=500)
    fig_sleep_minutes.update_layout(yaxis_title='Sleep Minutes', legend=dict(orientation="h",yanchor="bottom", y=1.02, xanchor="right", x=1, title_text=''), yaxis=dict(tickvals=[1,120,240,360,480,600,720], ticktext=[f"{m // 60}h" for m in [1,120,240,360,480,600,720]], title="Sleep Time (hours)"))
    if df_merged["Total Sleep Minutes"].dtype != object:
        fig_sleep_minutes.add_annotation(x=df_merged.iloc[df_merged["Total Sleep Minutes"].idxmax()]["Date"], y=df_merged["Total Sleep Minutes"].max(), text=str(format_minutes(df_merged["Total Sleep Minutes"].max())), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_sleep_minutes.add_annotation(x=df_merged.iloc[df_merged["Total Sleep Minutes"].idxmin()]["Date"], y=df_merged["Total Sleep Minutes"].min(), text=str(format_minutes(df_merged["Total Sleep Minutes"].min())), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    fig_sleep_minutes.add_hline(y=df_merged["Total Sleep Minutes"].mean(), line_dash="dot",annotation_text="Average : " + str(format_minutes(int(df_merged["Total Sleep Minutes"].mean()))), annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    fig_sleep_minutes.update_xaxes(rangeslider_visible=True,range=[dates_str_list[-30], dates_str_list[-1]],rangeslider_range=[dates_str_list[0], dates_str_list[-1]])
    sleep_summary_df = calculate_table_data(df_merged, "Total Sleep Minutes")
    sleep_summary_table = dash_table.DataTable(sleep_summary_df.to_dict('records'), [{"name": i, "id": i} for i in sleep_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#636efa','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_sleep_regularity = px.bar(df_merged, x="Date", y="Total Sleep Seconds", base="Sleep Start Time Seconds", title="<b>Sleep Regularity<br><br><sup>The chart time here is always in local time ( Independent of timezone changes )</sup></b>", labels={"Total Sleep Seconds":"Time of Day ( HH:MM )"})
    fig_sleep_regularity.update_layout(yaxis = dict(tickmode = 'array',tickvals = list(range(0, 120000, 10000)),ticktext = list(map(seconds_to_tick_label, list(range(0, 120000, 10000))))))
    fig_sleep_regularity.add_hline(y=df_merged["Sleep Start Time Seconds"].mean(), line_dash="dot",annotation_text="Sleep Start Time Trend : "+ str(seconds_to_tick_label(int(df_merged["Sleep Start Time Seconds"].mean()))), annotation_position="bottom right", annotation_bgcolor="#0a3024", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    fig_sleep_regularity.add_hline(y=df_merged["Sleep End Time Seconds"].mean(), line_dash="dot",annotation_text="Sleep End Time Trend : " + str(seconds_to_tick_label(int(df_merged["Sleep End Time Seconds"].mean()))), annotation_position="top left", annotation_bgcolor="#5e060d", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    
    # New visualizations
    # HRV
    hrv_avg = {'overall': round(df_merged["HRV"].mean(),1), '30d': round(df_merged["HRV"].tail(30).mean(),1)}
    fig_hrv = px.line(df_merged, x="Date", y="HRV", line_shape="spline", color_discrete_sequence=["#ff6692"], title=f"<b>Heart Rate Variability (HRV)<br><br><sup>Overall average : {hrv_avg['overall']} ms | Last 30d average : {hrv_avg['30d']} ms</sup></b><br><br><br>", labels={"HRV": "HRV (ms)"})
    if df_merged["HRV"].dtype != object and df_merged["HRV"].notna().any():
        fig_hrv.add_annotation(x=df_merged.iloc[df_merged["HRV"].idxmax()]["Date"], y=df_merged["HRV"].max(), text=str(df_merged["HRV"].max()), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_hrv.add_annotation(x=df_merged.iloc[df_merged["HRV"].idxmin()]["Date"], y=df_merged["HRV"].min(), text=str(df_merged["HRV"].min()), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_hrv.add_hline(y=df_merged["HRV"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["HRV"].mean(), 1)) + " ms", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    hrv_summary_df = calculate_table_data(df_merged, "HRV")
    hrv_summary_table = dash_table.DataTable(hrv_summary_df.to_dict('records'), [{"name": i, "id": i} for i in hrv_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#a8326b','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Breathing Rate
    breathing_avg = {'overall': round(df_merged["Breathing Rate"].mean(),1), '30d': round(df_merged["Breathing Rate"].tail(30).mean(),1)}
    fig_breathing = px.line(df_merged, x="Date", y="Breathing Rate", line_shape="spline", color_discrete_sequence=["#00d4ff"], title=f"<b>Breathing Rate<br><br><sup>Overall average : {breathing_avg['overall']} bpm | Last 30d average : {breathing_avg['30d']} bpm</sup></b><br><br><br>", labels={"Breathing Rate": "Breaths per Minute"})
    if df_merged["Breathing Rate"].dtype != object and df_merged["Breathing Rate"].notna().any():
        fig_breathing.add_annotation(x=df_merged.iloc[df_merged["Breathing Rate"].idxmax()]["Date"], y=df_merged["Breathing Rate"].max(), text=str(df_merged["Breathing Rate"].max()), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_breathing.add_annotation(x=df_merged.iloc[df_merged["Breathing Rate"].idxmin()]["Date"], y=df_merged["Breathing Rate"].min(), text=str(df_merged["Breathing Rate"].min()), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_breathing.add_hline(y=df_merged["Breathing Rate"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["Breathing Rate"].mean(), 1)) + " bpm", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    breathing_summary_df = calculate_table_data(df_merged, "Breathing Rate")
    breathing_summary_table = dash_table.DataTable(breathing_summary_df.to_dict('records'), [{"name": i, "id": i} for i in breathing_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#007a8c','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Cardio Fitness Score with error handling
    try:
        # Convert to numeric, coercing errors to NaN
        df_merged["Cardio Fitness Score"] = pd.to_numeric(df_merged["Cardio Fitness Score"], errors='coerce')
        cardio_fitness_avg = {'overall': round(df_merged["Cardio Fitness Score"].mean(),1), '30d': round(df_merged["Cardio Fitness Score"].tail(30).mean(),1)}
        fig_cardio_fitness = px.line(df_merged, x="Date", y="Cardio Fitness Score", line_shape="spline", color_discrete_sequence=["#ff9500"], title=f"<b>Cardio Fitness Score (VO2 Max)<br><br><sup>Overall average : {cardio_fitness_avg['overall']} | Last 30d average : {cardio_fitness_avg['30d']}</sup></b><br><br><br>")
        if df_merged["Cardio Fitness Score"].notna().any():
            fig_cardio_fitness.add_annotation(x=df_merged.iloc[df_merged["Cardio Fitness Score"].idxmax()]["Date"], y=df_merged["Cardio Fitness Score"].max(), text=str(round(df_merged["Cardio Fitness Score"].max(), 1)), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
            fig_cardio_fitness.add_annotation(x=df_merged.iloc[df_merged["Cardio Fitness Score"].idxmin()]["Date"], y=df_merged["Cardio Fitness Score"].min(), text=str(round(df_merged["Cardio Fitness Score"].min(), 1)), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
            fig_cardio_fitness.add_hline(y=df_merged["Cardio Fitness Score"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["Cardio Fitness Score"].mean(), 1)), annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
        cardio_fitness_summary_df = calculate_table_data(df_merged, "Cardio Fitness Score")
        cardio_fitness_summary_table = dash_table.DataTable(cardio_fitness_summary_df.to_dict('records'), [{"name": i, "id": i} for i in cardio_fitness_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#995500','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    except Exception as e:
        print(f"Error processing Cardio Fitness Score: {e}")
        fig_cardio_fitness = px.line(title="Cardio Fitness Score (No Data)")
        cardio_fitness_summary_table = html.P("No cardio fitness data available", style={'text-align': 'center', 'color': '#888'})
    
    # Temperature
    temperature_avg = {'overall': round(df_merged["Temperature"].mean(),2), '30d': round(df_merged["Temperature"].tail(30).mean(),2)}
    fig_temperature = px.line(df_merged, x="Date", y="Temperature", line_shape="spline", color_discrete_sequence=["#ff5733"], title=f"<b>Temperature Variation<br><br><sup>Overall average : {temperature_avg['overall']}°F | Last 30d average : {temperature_avg['30d']}°F</sup></b><br><br><br>")
    if df_merged["Temperature"].dtype != object and df_merged["Temperature"].notna().any():
        fig_temperature.add_annotation(x=df_merged.iloc[df_merged["Temperature"].idxmax()]["Date"], y=df_merged["Temperature"].max(), text=str(df_merged["Temperature"].max()), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_temperature.add_annotation(x=df_merged.iloc[df_merged["Temperature"].idxmin()]["Date"], y=df_merged["Temperature"].min(), text=str(df_merged["Temperature"].min()), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_temperature.add_hline(y=df_merged["Temperature"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["Temperature"].mean(), 2)) + "°F", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    temperature_summary_df = calculate_table_data(df_merged, "Temperature")
    temperature_summary_table = dash_table.DataTable(temperature_summary_df.to_dict('records'), [{"name": i, "id": i} for i in temperature_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#992211','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Active Zone Minutes
    azm_avg = {'overall': round(df_merged["Active Zone Minutes"].mean(),1), '30d': round(df_merged["Active Zone Minutes"].tail(30).mean(),1)}
    fig_azm = px.bar(df_merged, x="Date", y="Active Zone Minutes", color_discrete_sequence=["#ffcc00"], title=f"<b>Active Zone Minutes<br><br><sup>Overall average : {azm_avg['overall']} minutes | Last 30d average : {azm_avg['30d']} minutes</sup></b><br><br><br>")
    if df_merged["Active Zone Minutes"].dtype != object and df_merged["Active Zone Minutes"].notna().any():
        fig_azm.add_hline(y=df_merged["Active Zone Minutes"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["Active Zone Minutes"].mean(), 1)) + " minutes", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.8, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    azm_summary_df = calculate_table_data(df_merged, "Active Zone Minutes")
    azm_summary_table = dash_table.DataTable(azm_summary_df.to_dict('records'), [{"name": i, "id": i} for i in azm_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#997700','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Calories and Distance
    calories_avg = {'overall': int(df_merged["Calories"].mean()), '30d': int(df_merged["Calories"].tail(30).mean())}
    fig_calories = px.bar(df_merged, x="Date", y="Calories", color_discrete_sequence=["#ff3366"], title=f"<b>Daily Calories Burned<br><br><sup>Overall average : {calories_avg['overall']} cal | Last 30d average : {calories_avg['30d']} cal</sup></b><br><br><br>")
    if df_merged["Calories"].dtype != object and df_merged["Calories"].notna().any():
        fig_calories.add_hline(y=df_merged["Calories"].mean(), line_dash="dot",annotation_text="Average : " + str(int(df_merged["Calories"].mean())) + " cal", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.8, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    
    distance_avg = {'overall': round(df_merged["Distance"].mean(),2), '30d': round(df_merged["Distance"].tail(30).mean(),2)}
    fig_distance = px.bar(df_merged, x="Date", y="Distance", color_discrete_sequence=["#33ccff"], title=f"<b>Daily Distance<br><br><sup>Overall average : {distance_avg['overall']} miles | Last 30d average : {distance_avg['30d']} miles</sup></b><br><br><br>", labels={"Distance": "Distance (miles)"})
    if df_merged["Distance"].dtype != object and df_merged["Distance"].notna().any():
        fig_distance.add_hline(y=df_merged["Distance"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["Distance"].mean(), 2)) + " miles", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.8, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    
    calories_summary_df = calculate_table_data(df_merged, "Calories")
    calories_summary_table = dash_table.DataTable(calories_summary_df.to_dict('records'), [{"name": i, "id": i} for i in calories_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#991133','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Floors
    floors_avg = {'overall': int(df_merged["Floors"].mean()), '30d': int(df_merged["Floors"].tail(30).mean())}
    fig_floors = px.bar(df_merged, x="Date", y="Floors", color_discrete_sequence=["#9966ff"], title=f"<b>Daily Floors Climbed<br><br><sup>Overall average : {floors_avg['overall']} floors | Last 30d average : {floors_avg['30d']} floors</sup></b><br><br><br>")
    if df_merged["Floors"].dtype != object and df_merged["Floors"].notna().any():
        fig_floors.add_hline(y=df_merged["Floors"].mean(), line_dash="dot",annotation_text="Average : " + str(int(df_merged["Floors"].mean())) + " floors", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.8, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    floors_summary_df = calculate_table_data(df_merged, "Floors")
    floors_summary_table = dash_table.DataTable(floors_summary_df.to_dict('records'), [{"name": i, "id": i} for i in floors_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#663399','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Exercise Log with Enhanced Data
    exercise_data = []
    activity_types = set(['All'])
    workout_dates_for_dropdown = []  # For drill-down selector
    activities_by_date = {}  # Store activities by date for drill-down
    
    for activity in response_activities.get('activities', []):
        try:
            activity_date = datetime.strptime(activity['startTime'][:10], '%Y-%m-%d').strftime("%Y-%m-%d")
            if activity_date >= start_date and activity_date <= end_date:
                activity_name = activity.get('activityName', 'N/A')
                activity_types.add(activity_name)
                exercise_data.append({
                    'Date': activity_date,
                    'Activity': activity_name,
                    'Duration (min)': activity.get('duration', 0) // 60000,
                    'Calories': activity.get('calories', 0),
                    'Avg HR': activity.get('averageHeartRate', 'N/A'),
                    'Steps': activity.get('steps', 'N/A'),
                    'Distance (mi)': round(activity.get('distance', 0) * 0.621371, 2) if activity.get('distance') else 'N/A'
                })
                
                # Store for drill-down
                if activity_date not in activities_by_date:
                    activities_by_date[activity_date] = []
                    workout_dates_for_dropdown.append({'label': f"{activity_date} - {activity_name}", 'value': activity_date})
                activities_by_date[activity_date].append(activity)
        except:
            pass
    
    # Exercise type filter options
    exercise_filter_options = [{'label': activity_type, 'value': activity_type} for activity_type in sorted(activity_types)]
    
    if exercise_data:
        exercise_df = pd.DataFrame(exercise_data)
        exercise_log_table = dash_table.DataTable(
            exercise_df.to_dict('records'), 
            [{"name": i, "id": i} for i in exercise_df.columns], 
            style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], 
            style_header={'backgroundColor': '#336699','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, 
            style_cell={'textAlign': 'center'},
            page_size=20
        )
    else:
        exercise_df = pd.DataFrame()
        exercise_log_table = html.P("No exercise activities logged in this period.", style={'text-align': 'center', 'color': '#888'})
    
    # Phase 3B: Sleep Quality Analysis - Use cached Fitbit sleep scores
    print("🗄️ Checking cache for sleep scores...")
    
    # Check which dates are missing from cache
    missing_dates = cache.get_missing_dates(start_date, end_date, metric_type='sleep')
    
    if missing_dates:
        # Limit to 30 dates at a time to avoid rate limits
        dates_to_fetch = missing_dates[:30]  # Start with last 30 days
        print(f"📥 Fetching {len(dates_to_fetch)} missing sleep scores from API...")
        fetched = populate_sleep_score_cache(dates_to_fetch, headers)
        print(f"✅ Successfully cached {fetched} new sleep scores")
        
        if len(missing_dates) > 30:
            print(f"ℹ️ {len(missing_dates) - 30} older dates will be fetched in future reports")
    else:
        print("✅ All sleep scores already cached!")
    
    # Now build sleep scores from cache
    sleep_scores = []
    sleep_stages_totals = {'Deep': 0, 'Light': 0, 'REM': 0, 'Wake': 0}
    sleep_dates_for_dropdown = []  # For drill-down selector
    
    for date_str in dates_str_list:
        # Try cache first
        cached_sleep = cache.get_sleep_data(date_str)
        if cached_sleep and cached_sleep['sleep_score'] is not None:
            sleep_scores.append({'Date': date_str, 'Score': cached_sleep['sleep_score']})
            sleep_dates_for_dropdown.append({'label': date_str, 'value': date_str})
            
            # Use cached sleep stage data if available
            if cached_sleep['deep']:
                sleep_stages_totals['Deep'] += cached_sleep['deep']
            if cached_sleep['light']:
                sleep_stages_totals['Light'] += cached_sleep['light']
            if cached_sleep['rem']:
                sleep_stages_totals['REM'] += cached_sleep['rem']
            if cached_sleep['wake']:
                sleep_stages_totals['Wake'] += cached_sleep['wake']
        elif date_str in sleep_record_dict:
            # Fallback to sleep_record_dict if not in cache yet
            sleep_data = sleep_record_dict[date_str]
            fitbit_score = sleep_data.get('sleep_score')
            if fitbit_score is not None:
                sleep_scores.append({'Date': date_str, 'Score': fitbit_score})
                sleep_dates_for_dropdown.append({'label': date_str, 'value': date_str})
            
            # Accumulate stage totals for pie chart
            sleep_stages_totals['Deep'] += sleep_data.get('deep', 0)
            sleep_stages_totals['Light'] += sleep_data.get('light', 0)
            sleep_stages_totals['REM'] += sleep_data.get('rem', 0)
            sleep_stages_totals['Wake'] += sleep_data.get('wake', 0)
    
    # Sleep Score Chart
    if sleep_scores:
        sleep_score_df = pd.DataFrame(sleep_scores)
        fig_sleep_score = px.line(sleep_score_df, x='Date', y='Score', 
                                   title='Sleep Quality Score (0-100)',
                                   markers=True)
        fig_sleep_score.update_layout(yaxis_range=[0, 100])
        fig_sleep_score.add_hline(y=75, line_dash="dot", line_color="green", 
                                   annotation_text="Good Sleep", annotation_position="right")
    else:
        fig_sleep_score = px.line(title='Sleep Quality Score (No Data)')
    
    # Sleep Stages Pie Chart
    if sum(sleep_stages_totals.values()) > 0:
        stages_df = pd.DataFrame([{'Stage': k, 'Minutes': v} for k, v in sleep_stages_totals.items() if v > 0])
        fig_sleep_stages_pie = px.pie(stages_df, values='Minutes', names='Stage',
                                       title='Average Sleep Stage Distribution',
                                       color='Stage',
                                       color_discrete_map={'Deep': '#084466', 'Light': '#1e9ad6', 
                                                          'REM': '#4cc5da', 'Wake': '#fd7676'})
    else:
        fig_sleep_stages_pie = px.pie(title='Sleep Stages (No Data)')
    
    # Phase 4: Exercise-Sleep Correlation
    correlation_data = []
    for i, date_str in enumerate(dates_str_list[:-1]):  # Skip last day
        # Check if there was exercise on this day
        exercise_calories = sum([ex['Calories'] for ex in exercise_data if ex['Date'] == date_str])
        exercise_duration = sum([ex['Duration (min)'] for ex in exercise_data if ex['Date'] == date_str])
        
        # Get next day's sleep
        next_date = dates_str_list[i + 1]
        if next_date in sleep_record_dict:
            sleep_data = sleep_record_dict[next_date]
            correlation_data.append({
                'Date': date_str,
                'Exercise Calories': exercise_calories,
                'Exercise Duration (min)': exercise_duration,
                'Next Day Sleep (min)': sleep_data.get('total_sleep', 0),
                'Deep Sleep %': (sleep_data.get('deep', 0) / sleep_data.get('total_sleep', 1) * 100) if sleep_data.get('total_sleep', 0) > 0 else 0
            })
    
    if correlation_data and len(correlation_data) > 3:
        corr_df = pd.DataFrame(correlation_data)
        corr_df = corr_df[corr_df['Exercise Calories'] > 0]  # Only days with exercise
        
        if len(corr_df) > 0:
            fig_correlation = px.scatter(corr_df, x='Exercise Calories', y='Next Day Sleep (min)',
                                        size='Exercise Duration (min)', hover_data=['Date'],
                                        title='Exercise Impact on Next Day Sleep',
                                        trendline="ols")
            fig_correlation.update_layout(xaxis_title="Exercise Calories Burned",
                                         yaxis_title="Next Day Sleep Duration (min)")
            
            # Calculate correlation coefficient
            if len(corr_df) >= 3:
                corr_coef = corr_df['Exercise Calories'].corr(corr_df['Next Day Sleep (min)'])
                avg_exercise_sleep = corr_df[corr_df['Exercise Calories'] > 100]['Next Day Sleep (min)'].mean()
                avg_no_exercise_sleep = corr_df[corr_df['Exercise Calories'] <= 100]['Next Day Sleep (min)'].mean()
                
                correlation_insights = html.Div([
                    html.H5("🔍 Insights:", style={'margin-bottom': '15px'}),
                    html.P(f"📊 Correlation between exercise and next-day sleep: {corr_coef:.2f}" + 
                          (" (Positive - More exercise correlates with better sleep!)" if corr_coef > 0.3 else 
                           " (Negative - Heavy exercise may be affecting sleep)" if corr_coef < -0.3 else 
                           " (Weak correlation)")),
                    html.P(f"💪 Average sleep after workout days: {avg_exercise_sleep:.0f} minutes" if not pd.isna(avg_exercise_sleep) else ""),
                    html.P(f"😴 Average sleep on rest days: {avg_no_exercise_sleep:.0f} minutes" if not pd.isna(avg_no_exercise_sleep) else ""),
                    html.P(f"✨ Best practice: Your data suggests exercising in the {'morning/afternoon' if corr_coef > 0 else 'earlier hours'} for optimal sleep quality.")
                ])
            else:
                correlation_insights = html.P("Need more exercise data for meaningful insights (minimum 3 workout days).")
        else:
            fig_correlation = px.scatter(title='Exercise-Sleep Correlation (No Exercise Data)')
            correlation_insights = html.P("No exercise activities found in this period.")
    else:
        fig_correlation = px.scatter(title='Exercise-Sleep Correlation (Insufficient Data)')
        correlation_insights = html.P("Need more data points for correlation analysis. Try a longer date range or log more workouts!")
    
    return report_title, report_dates_range, generated_on_date, fig_rhr, rhr_summary_table, fig_steps, fig_steps_heatmap, steps_summary_table, fig_activity_minutes, fat_burn_summary_table, cardio_summary_table, peak_summary_table, fig_weight, weight_summary_table, fig_spo2, spo2_summary_table, fig_sleep_minutes, fig_sleep_regularity, sleep_summary_table, [{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled': False}], fig_hrv, hrv_summary_table, fig_breathing, breathing_summary_table, fig_cardio_fitness, cardio_fitness_summary_table, fig_temperature, temperature_summary_table, fig_azm, azm_summary_table, fig_calories, fig_distance, calories_summary_table, fig_floors, floors_summary_table, exercise_filter_options, exercise_log_table, workout_dates_for_dropdown, fig_sleep_score, fig_sleep_stages_pie, sleep_dates_for_dropdown, fig_correlation, correlation_insights, ""

if __name__ == '__main__':
    app.run_server(debug=True)



# %%
