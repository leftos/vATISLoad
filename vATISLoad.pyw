#####################################################################
############################# vATISLoad #############################
#####################################################################

DISABLE_AUTOUPDATES = False     # Set to True to disable auto-updates
SHUTDOWN_LIMIT = 60 * 5         # Time delay to exit script
AUTO_SELECT_FACILITY = False    # Enable/disable auto-select facility
RUN_UPDATE = False              # Set to False for testing

#####################################################################

import subprocess, sys, os, time, json, re, uuid, ctypes, asyncio, difflib, winreg, argparse, logging
from datetime import datetime, timezone

# Parse --debug flag early for logging setup
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument('--debug', action='store_true')
_early_args, _ = _parser.parse_known_args()

# Configure logging based on --debug flag
if _early_args.debug:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )
else:
    logging.basicConfig(level=logging.CRITICAL + 1)  # Effectively disable logging
log = logging.getLogger('vATISLoad')

log.info("vATISLoad starting...")

import importlib.util as il
missing_libs = []
for lib in ['requests', 'websockets', 'psutil', 'pygetwindow']:
    if il.find_spec(lib) is None:
        missing_libs.append(lib)

if len(missing_libs) > 0:
    log.info(f"Installing missing libraries: {missing_libs}")
    os.system('cmd /K "cls & echo Updating required libraries for vATISLoad.' +
        ' & echo Please wait a few minutes for libraries to install. & echo.' +
        ' & echo If this does not work, try using vATISLoad_library_installer.py (see the vATISLoad README).' +
        ' & timeout 15 & exit"')

    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip'])
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'requests']);
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'websockets']);
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'psutil']);
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'pygetwindow']);

import requests, websockets, psutil, pygetwindow

def update_vATISLoad():
    log.info("Checking for updates...")
    online_file = ''
    url = 'https://raw.githubusercontent.com/glott/vATISLoad/refs/heads/main/vATISLoad.pyw'
    try:
        online_file = requests.get(url).text.split('\n')
    except Exception as e:
        log.error(f"Failed to fetch update: {e}")
        return

    up_to_date = True
    with open(sys.argv[0], 'r') as FileObj:
        i = 0
        for line in FileObj:
            if ('DISABLE_AUTOUPDATES =' in line or 'RUN_UPDATE =' in line 
                or 'SHUTDOWN_LIMIT =' in line or 'AUTO_SELECT_FACILITY' in line) and i < 10:
                pass
            elif i > len(online_file) or len(line.strip()) != len(online_file[i].strip()):
                up_to_date = False
                break
            i += 1

    if up_to_date:
        return

    log.info("Update available, downloading...")
    try:
        os.rename(sys.argv[0], sys.argv[0] + '.bak')
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(sys.argv[0], 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192): 
                    f.write(chunk)

        os.remove(sys.argv[0] + '.bak')

    except Exception as e:
        log.error(f"Update failed: {e}")
        if not os.path.isfile(sys.argv[0]) and os.path.isfile(sys.argv[0] + '.bak'):
            os.rename(sys.argv[0] + '.bak', sys.argv[0])

    log.info("Restarting with new version...")
    os.execv(sys.executable, ['python'] + sys.argv)

def determine_active_callsign(return_artcc_only=False):
    crc_path = ''
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'SOFTWARE\\CRC')
        crc_path, value_type = winreg.QueryValueEx(key, 'Install_Dir')
    except FileNotFoundError as ignored:
        crc_path = os.path.join(os.getenv('LOCALAPPDATA'), 'CRC')
    
    crc_profiles = os.path.join(crc_path, 'Profiles')
    crc_name = ''
    crc_data = {}
    crc_lastused_time = '2020-01-01T08:00:00'
    try:
        for filename in os.listdir(crc_profiles):
            if filename.endswith('.json'): 
                file_path = os.path.join(crc_profiles, filename)
                with open(file_path, 'r') as f:
                    data = json.load(f)
                    dt1 = datetime.strptime(crc_lastused_time, '%Y-%m-%dT%H:%M:%S')
                    if 'LastUsedAt' not in data or data['LastUsedAt'] == None:
                        continue
                    dt2 = datetime.strptime(data['LastUsedAt'].split('.')[0].replace('Z',''), '%Y-%m-%dT%H:%M:%S')
                    if dt2 > dt1:
                        crc_lastused_time = data['LastUsedAt'].split('.')[0].replace('Z','')
                        crc_name = data['Name']
                        crc_data = data
    except Exception as e:
        log.error(f"Error reading CRC profiles: {e}")
        return None

    if return_artcc_only:
        return crc_data.get('ArtccId', None)

    try:
        lastPos = crc_data['LastUsedPositionId']
        crc_ARTCC = os.path.join(crc_path, 'ARTCCs') + os.sep + crc_data['ArtccId'] + '.json'
        with open(crc_ARTCC, 'r') as f:
            data = json.load(f)

        pos = determine_position_from_id(data['facility']['positions'], lastPos)
        if pos is not None:
            log.info(f"Active callsign: {pos[0]}_{pos[1]}")
            return pos

        for child1 in data['facility']['childFacilities']:
            pos = determine_position_from_id(child1['positions'], lastPos)
            if pos is not None:
                log.info(f"Active callsign: {pos[0]}_{pos[1]}")
                return pos

            for child2 in child1['childFacilities']:
                pos = determine_position_from_id(child2['positions'], lastPos)
                if pos is not None:
                    log.info(f"Active callsign: {pos[0]}_{pos[1]}")
                    return pos

    except Exception as e:
        log.error(f"Error determining position: {e}")

    return None

async def auto_select_facility():
    artcc = determine_active_callsign(return_artcc_only=True)
    if artcc is None:
        return
    
    if not AUTO_SELECT_FACILITY and not artcc in ['ZOA', 'ZMA', 'ZDC']:
        return

    # Determine if CRC is open and a profile is loaded
    crc_found = False
    for win in [w.title for w in pygetwindow.getAllWindows()]:
        if 'CRC : 1' in win:
            crc_found = True

    if not crc_found:
        return
    
    try:
        async with websockets.connect('ws://127.0.0.1:49082/', close_timeout=0.01) as websocket:
            # Determine if any vATIS profile matches ARTCC
            await websocket.send(json.dumps({'type': 'getProfiles'}))
            m = json.loads(await asyncio.wait_for(websocket.recv(), timeout=0.25))['profiles']
            
            match_id = ''
            for p in m:
                if artcc in p['name']:
                    match_id = p['id']
        
            if len(match_id) < 0:
                return
            
            # Determine if current profile is already the desired profile
            await websocket.send(json.dumps({'type': 'getActiveProfile'}))
            m = json.loads(await asyncio.wait_for(websocket.recv(), timeout=0.25))
            
            if 'id' in m:
                # Do not select a profile if current profile is already selected
                if m['id'] == match_id:
                    return
            
            # Load new profile
            log.info(f"Auto-selecting profile for {artcc}")
            await websocket.send(json.dumps({'type': 'loadProfile', 'value': {'id': match_id}}))
            await asyncio.sleep(1)

    except Exception:
        pass

async def try_websocket(shutdown=RUN_UPDATE, limit=SHUTDOWN_LIMIT, initial=False):
    t0 = time.time()
    for i in range(0, 250):
        if initial and i < 30:
            await auto_select_facility()
        
        t1 = time.time()
        elapsed = t1 - t0
        if elapsed > limit:
            log.warning(f"Websocket timeout after {elapsed:.0f}s")
            if shutdown:
                sys.exit()
            return
        try:
            async with websockets.connect('ws://127.0.0.1:49082/', close_timeout=0.01) as websocket:
                await websocket.send(json.dumps({'type': 'getStations'}))
                try:
                    m = json.loads(await asyncio.wait_for(websocket.recv(), timeout=1))
                    if time.time() - t0 > 5:
                        await asyncio.sleep(1)
                    if m['type'] != 'stations':
                        await asyncio.sleep(0.5)
                        continue
                    return
                except Exception as ignored:
                    pass
        except Exception as ignored:
            dt = time.time() - t1
            if dt < 1:
                await asyncio.sleep(1 - dt)
            pass

async def get_datis_stations(initial=False):
    await try_websocket(initial=initial)
    
    data = {}
    async with websockets.connect('ws://127.0.0.1:49082/', close_timeout=0.01) as websocket:
        await websocket.send(json.dumps({'type': 'getStations'}))
        m = json.loads(await websocket.recv())
        
        not_stations = False
        while m['type'] != 'stations':
            not_stations = True
            await asyncio.sleep(0.1)
            await websocket.send(json.dumps({'type': 'getStations'}))
            m = json.loads(await websocket.recv())
        
        if not_stations:
            await asyncio.sleep(0.5)
            await websocket.send(json.dumps({'type': 'getStations'}))
            m = json.loads(await websocket.recv())

        for s in m['stations']:
            name = s['name']

            if s['atisType'] == 'Arrival':
                name += '_A'
            elif s['atisType'] == 'Departure':
                name += '_D'
            
            if 'D-ATIS' in s['presets']:
                data[name] = s['id']

    log.info(f"Found {len(data)} D-ATIS stations: {list(data.keys())}")
    return data

def get_atis_replacements(stations):
    stations = list(set(value.replace('_A', '').replace('_D', '') for value in stations))

    config = {}
    try:
        url = 'https://raw.githubusercontent.com/glott/vATISLoad/refs/heads/main/vATISLoadConfig.json'
        config = json.loads(requests.get(url).text)
    except Exception as e:
        log.error(f"Failed to fetch config: {e}")

    if 'replacements' not in config:
        return {}

    replacements = {}
    for a in config['replacements']:
        if a in stations:
            replacements[a] = config['replacements'][a]

    if len(replacements) > 0:
        log.info(f"Loaded replacements for {len(replacements)} airports")
    return replacements

def get_user_config():
    config = {}
    config_path = os.path.join(os.path.dirname(sys.argv[0]), 'vATISLoadUserConfig.json')
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
            log.info(f"Loaded user config for {len(config)} airports")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error(f"Error loading user config: {e}")
    return config

def apply_user_modifications(airport, conditions, notams, user_config):
    if airport not in user_config:
        return conditions, notams

    cfg = user_config[airport]

    # Apply conditions modifications
    if 'conditions' in cfg:
        for text in cfg['conditions'].get('remove', []):
            conditions = conditions.replace(text, '')
        append_text = cfg['conditions'].get('append', '')
        if append_text:
            if conditions and not conditions.endswith(' '):
                conditions += ' '
            conditions += append_text

    # Apply notams modifications
    if 'notams' in cfg:
        for text in cfg['notams'].get('remove', []):
            notams = notams.replace(text, '')
        append_text = cfg['notams'].get('append', '')
        if append_text:
            if notams and not notams.endswith(' '):
                notams += ' '
            notams += append_text

    # Clean up extra spaces
    conditions = re.sub(r'\s+', ' ', conditions).strip()
    notams = re.sub(r'\s+', ' ', notams).strip()

    return conditions, notams

async def get_contractions(station):
    try:
        async with websockets.connect('ws://127.0.0.1:49082/', close_timeout=0.01) as websocket:
            if '_D' in station:
                payload = {'station': station[0:4], 'atisType': 'Departure'}
            elif '_A' in station:
                payload = {'station': station[0:4], 'atisType': 'Arrival'}
            else:
                payload = {'station': station[0:4]}
            await websocket.send(json.dumps({'type': 'getContractions', 'value': payload}))
            m = json.loads(await asyncio.wait_for(websocket.recv(), timeout=0.25))
    

            c = {}
            contractions = m['stations'][0]['contractions']
            for cont in contractions:
                c[contractions[cont]['text']] = '@' + cont
            
            c = dict(sorted(c.items(), key=lambda item: len(item[0])))
            c = {key: c[key] for key in reversed(c)}

            return c
    except asyncio.TimeoutError:
        log.warning(f"Timeout getting contractions for {station}")
    except Exception as e:
        log.error(f"Error getting contractions for {station}: {e}")

    return {}

def get_datis_data():
    log.info("Fetching D-ATIS data...")
    data = {}
    try:
        url = 'https://atis.info/api/all'
        data = json.loads(requests.get(url, timeout=2.5).text)
        log.info(f"Fetched D-ATIS for {len(data)} airports")
    except Exception as e:
        log.error(f"Failed to fetch D-ATIS data: {e}")
        os.system('cmd /K "cls & echo Unable to fetch D-ATIS data. & timeout 5 & exit"')

    return data

async def get_datis(station, atis_data, replacements):
    atis_type = 'combined'
    if '_A' in station:
        atis_type = 'arr'
    elif '_D' in station:
        atis_type = 'dep'

    atis_info = ['D-ATIS NOT AVBL.', '']
    if 'error' in atis_data:
        log.warning(f"{station}: D-ATIS data has error")
        return atis_info

    datis = ''
    for a in atis_data:
        if a['airport'] != station[0:4] or a['type'] != atis_type:
            continue
        datis = a['datis']

        # Ignore D-ATIS more than 1.75 hours old
        try: 
            t_updated = datetime.strptime(a['updatedAt'][:26], "%Y-%m-%dT%H:%M:%S.%f")
            t_updated = t_updated.replace(tzinfo=timezone.utc)
            t_now = datetime.now(timezone.utc)

            age_hours = (t_now - t_updated).total_seconds() / 3600
            if age_hours > 1.75:
                log.warning(f"{station}: D-ATIS too old ({age_hours:.1f}h)")
                return atis_info
        except Exception:
            pass

    if len(datis) == 0:
        return atis_info

    # Strip beginning and ending D-ATIS text
    datis = '. '.join(datis.split('. ')[2:])
    datis = re.sub(' ...ADVS YOU HAVE.*', '', datis)
    datis = datis.replace('NOTICE TO AIR MISSIONS, NOTAMS. ', 'NOTAMS... ') \
        .replace('NOTICE TO AIR MISSIONS. ', 'NOTAMS... ') \
        .replace('NOTICE TO AIR MEN. ', 'NOTAMS... ') \
        .replace('NOTICE TO AIRMEN. ', 'NOTAMS... ') \
        .replace('NOTAMS. ', 'NOTAMS... ') \
        .replace('NOTAM. ', 'NOTAMS... ')

    # Replace defined replacements
    for r in replacements:
        if '%r' in replacements[r]:
            datis = re.sub(r + '[,.;]{0,2}', replacements[r].replace('%r', ''), datis)
        else:
            datis = re.sub(r + '[,.;]{0,2}', replacements[r], datis)
    datis = re.sub(r'\s+', ' ', datis).strip()

    # Clean up D-ATIS
    datis = datis.replace('...', '/./').replace('..', '.') \
        .replace('/./', '...').replace('  ', ' ').replace(' . ', '. ') \
        .replace(', ,', ',').replace(' ; ', '; ').replace(' .,', ' ,') \
        .replace(' , ', ', ').replace('., ', ', ').replace('&amp;', '&') \
        .replace(' ;.', '.').replace(' ;,', ',')

    # Replace contractions
    contractions = await get_contractions(station)
    for c, v in contractions.items():
        if not c.isdigit():
            datis = re.sub(r'(?<!@)\b' + c + r'\b,', v + ',', datis)
            datis = re.sub(r'(?<!@)\b' + c + r'\b\.', v + '.', datis)
            datis = re.sub(r'(?<!@)\b' + c + r'\b ', v + ' ', datis)
            datis = re.sub(r'(?<!@)\b' + c + r'\b;', v + ';', datis)

    # Split at NOTAMs
    if 'NOTAMS... ' in datis:
        atis_info = datis.split('NOTAMS... ', maxsplit=1)
    else:
        atis_info = [datis, '']
    
    return atis_info

async def get_atis_statuses():
    await try_websocket()
    
    data = {}
    
    async with websockets.connect('ws://127.0.0.1:49082/', close_timeout=0.01) as websocket:
        for s, i in (await get_datis_stations()).items():
            await websocket.send(json.dumps({'type': 'getAtis', 'value': {'id': i}}))
            
            m = json.loads(await websocket.recv())['value']
            data[s] = m['networkConnectionStatus']

    connected = [k for k, v in data.items() if v == 'Connected']
    disconnected = [k for k, v in data.items() if v == 'Disconnected']
    other = {k: v for k, v in data.items() if v not in ['Connected', 'Disconnected']}
    log.info(f"Connected: {connected}, Disconnected: {disconnected}")
    if other:
        log.info(f"Other statuses: {other}")
    return data

async def get_num_connections():
    n = 0
    for k, v in (await get_atis_statuses()).items():
        if v == 'Connected':
            n =+ 1
    return n

async def configure_atises(connected_only=False, initial=False, temp_rep={}):
    log.info(f"Configuring ATISes (connected_only={connected_only})")
    stations = await get_datis_stations(initial=initial)
    replacements = get_atis_replacements(stations)
    atis_data = get_datis_data()
    user_config = get_user_config()

    atis_statuses = await get_atis_statuses()

    for k, v in temp_rep.items():
        for cont, cont_rep in (await get_contractions(k)).items():
            temp_rep[k] = [elem.replace(cont_rep, cont) for elem in temp_rep[k]]

    configured = []
    for s, i in stations.items():
        if connected_only and atis_statuses[s] != 'Connected':
            continue

        rep = []
        if s[0:4] in replacements:
            rep = replacements[s[0:4]].copy()
            if s in temp_rep:
                for tr in temp_rep[s]:
                    rep[tr.strip()] = ''
        
        v = {'id': i, 'preset': 'D-ATIS', 'syncAtisLetter': True}
        v['airportConditionsFreeText'], v['notamsFreeText'] = await get_datis(s, atis_data, rep)
        v['airportConditionsFreeText'], v['notamsFreeText'] = apply_user_modifications(
            s[0:4], v['airportConditionsFreeText'], v['notamsFreeText'], user_config)

        if connected_only and v['airportConditionsFreeText'] == 'D-ATIS NOT AVBL.':
            continue

        configured.append(s)
        payload = {'type': 'configureAtis', 'value': v}
        async with websockets.connect('ws://127.0.0.1:49082/', close_timeout=0.01) as websocket:
            await websocket.send(json.dumps(payload))

    log.info(f"Configured: {configured}")

def determine_position_from_id(positions, position_id):
    for p in positions:
        if p['id'] == position_id:
            c = p['callsign']
            idx1 = c.find('_')
            idx2 = c.rfind('_')
        
            if idx1 == -1 or idx2 == -1:
                return None
        
            prefix = c[:idx1]
            suffix = c[idx2 + 1:]
            return [prefix, suffix]
    
    return None

async def connect_atises(airport_override=None):
    log.info(f"Connecting ATISes (override={airport_override})")
    stations = await get_datis_stations()
    atis_statuses = await get_atis_statuses()
    disconnected_atises = [k for k, v in atis_statuses.items() if v == 'Disconnected']
    n_connected = len([k for k, v in atis_statuses.items() if v == 'Connected'])

    # If airport override is provided, filter to only those airports
    if airport_override is not None:
        # Normalize overrides to match ICAO codes (handle K prefix for US airports)
        normalized_overrides = []
        for a in airport_override:
            a_upper = a.upper()
            normalized_overrides.append(a_upper)
            # Add K-prefixed version if not already prefixed
            if not a_upper.startswith('K') and len(a_upper) == 3:
                normalized_overrides.append('K' + a_upper)
            # Add non-K version if K-prefixed
            if a_upper.startswith('K') and len(a_upper) == 4:
                normalized_overrides.append(a_upper[1:])

        # Filter disconnected stations to match override
        stations_temp = {}
        for da in disconnected_atises:
            airport_code = da[:4]  # Extract airport code (e.g., 'KSFO' from 'KSFO_A')
            if airport_code.upper() in normalized_overrides and da in stations:
                stations_temp[da] = stations[da]
        stations = stations_temp
        log.info(f"Filtered to airports: {list(stations.keys())}")
    else:
        # Original logic: auto-select based on active callsign
        active_callsign = determine_active_callsign()
        if active_callsign is not None:
            suf = active_callsign[1]
            if suf == 'TWR' or suf == 'GND' or suf == 'DEL' or suf == 'RMP':
                stations_temp = {}
                for da in disconnected_atises:
                    if active_callsign[0] in da and da in stations:
                        stations_temp[da] = stations[da]
                
                stations = stations_temp

    n = 0
    connected = []
    for s, i in stations.items():
        if n + n_connected >= 4:
            log.warning("Connection limit (4) reached")
            break

        if s not in disconnected_atises:
            continue

        payload = {'type': 'connectAtis', 'value': {'id': i}}
        async with websockets.connect('ws://127.0.0.1:49082/', close_timeout=0.01) as websocket:
            await websocket.send(json.dumps(payload))

            try:
                m = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                n += 1
                connected.append(s)
            except Exception:
                pass

    if connected:
        log.info(f"Connected: {connected}")

def kill_open_instances():
    prev_instances = {}
    current_pid = os.getpid()
    current_process = psutil.Process(current_pid)
    current_start = current_process.create_time()

    # Get parent PID to avoid killing it
    try:
        parent_pid = current_process.ppid()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        parent_pid = None

    log.info(f"Current process PID: {current_pid}, Parent PID: {parent_pid}")

    for q in psutil.process_iter():
        try:
            if 'python' in q.name():
                for parameter in q.cmdline():
                    if 'vATISLoad' in parameter and parameter.endswith('.pyw'):
                        q_create_time = q.create_time()
                        q_create_datetime = datetime.fromtimestamp(q_create_time)
                        prev_instances[q.pid] = {'process': q, 'start': q_create_datetime, 'start_ts': q_create_time}
                        log.info(f"Found vATISLoad instance: PID {q.pid}, started {q_create_datetime}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    prev_instances = dict(sorted(prev_instances.items(), key=lambda item: item[1]['start']))
    log.info(f"Sorted PIDs (oldest first): {list(prev_instances.keys())}")

    for i in range(0, len(prev_instances) - 1):
        k = list(prev_instances.keys())[i]
        # Skip current process
        if k == current_pid:
            log.warning(f"Skipping termination of current process PID {k}")
            continue
        # Skip parent process
        if k == parent_pid:
            log.warning(f"Skipping termination of parent process PID {k}")
            continue
        # Skip processes started within 1 second of current (likely related to same invocation)
        if abs(prev_instances[k]['start_ts'] - current_start) < 1.0:
            log.warning(f"Skipping PID {k} - started too close to current process")
            continue
        log.info(f"Terminating previous instance: PID {k}")
        prev_instances[k]['process'].terminate()

    time.sleep(0.5)  # Give terminated processes time to exit

def open_vATIS():
    log.info("open_vATIS() called")
    # Set 'autoFetchAtisLetter' to True
    config_path = os.getenv('LOCALAPPDATA') + '\\org.vatsim.vatis\\AppConfig.json'
    try:
        with open(config_path, 'r') as f:
            data = json.load(f)
            if 'autoFetchAtisLetter' in data:
                data['autoFetchAtisLetter'] = True
        with open(config_path, 'w') as f:
            json.dump(data, f, indent=2)
        log.info("vATIS config updated")
    except Exception as e:
        log.warning(f"Could not update vATIS config: {e}")

    # Check if vATIS is open
    log.info("Checking if vATIS is running...")
    try:
        for process in psutil.process_iter(['name']):
            try:
                if process.info['name'] == 'vATIS.exe':
                    log.info("vATIS already running")
                    return
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        log.error(f"Error checking processes: {e}")

    exe = os.getenv('LOCALAPPDATA') + '\\org.vatsim.vatis\\current\\vATIS.exe'
    log.info(f"Starting vATIS from: {exe}")
    try:
        subprocess.Popen(exe)
    except Exception as e:
        log.error(f"Failed to start vATIS: {e}")

async def get_connected_atis_data():
    stations = await get_datis_stations()
    atis_statuses = await get_atis_statuses()

    connected_atis_data = {}
    
    for station in [k for k, v in atis_statuses.items() if v == 'Connected']:
        payload = {'type': 'getAtis', 'value': {'id': stations[station]}}
        async with websockets.connect('ws://127.0.0.1:49082/', close_timeout=0.01) as websocket:
            await websocket.send(json.dumps(payload))

            m = json.loads(await websocket.recv())['value']
            connected_atis_data[station] = [m['airportConditions'], m['notams']]

    return connected_atis_data

async def disconnect_over_connection_limit(delay=True):
    if True:
        time.sleep(5)
    
    stations = await get_datis_stations()
    atis_statuses = await get_atis_statuses()
    connected_atises = [k for k, v in atis_statuses.items() if v == 'Connected']

    if len(connected_atises) <= 4 or SHUTDOWN_LIMIT == 346:
        return

    log.warning(f"Over connection limit ({len(connected_atises)} > 4), disconnecting excess")
    for i in range(4, len(connected_atises)):
        s, i = connected_atises[i], stations[connected_atises[i]]
        payload = {'type': 'disconnectAtis', 'value': {'id': i}}
        async with websockets.connect('ws://127.0.0.1:49082/', close_timeout=0.01) as websocket:
            await websocket.send(json.dumps(payload))

def find_deleted_portions(original, modified):
    sequence_matcher = difflib.SequenceMatcher(None, original, modified)
    
    deleted_portions = []
    for tag, i1, i2, j1, j2 in sequence_matcher.get_opcodes():
        if tag == 'delete':  
            deleted_portions.append(original[i1:i2])
    
    return deleted_portions

def compare_atis_data(prev_data, new_data):
    compared_output = {}

    for station in prev_data:
        if station not in new_data:
            continue
        
        conditionDiff = find_deleted_portions(prev_data[station][0], new_data[station][0])
        notamDiff = find_deleted_portions(prev_data[station][1], new_data[station][1])

        if len(conditionDiff) > 0 or len(notamDiff) > 0:
            compared_output[station] = conditionDiff + notamDiff
            log.info(f"Changes detected for {station}")

    return compared_output

async def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='vATIS Auto-Loader')
    parser.add_argument('--airports', nargs='+', metavar='ICAO',
                       help='Optional list of airport ICAO codes to activate (e.g., KSFO KOAK)')
    parser.add_argument('--debug', action='store_true',
                       help='Enable debug logging output')
    args = parser.parse_args()

    if args.airports:
        log.info(f"Airport override: {args.airports}")

    if RUN_UPDATE:
        update_vATISLoad()

    kill_open_instances()
    log.info("Previous instances handled, proceeding...")
    open_vATIS()
    await configure_atises(initial=True)
    await connect_atises(airport_override=args.airports)
    
    prev_data = await get_connected_atis_data()
    await disconnect_over_connection_limit()

    k, temp_rep = 0, []
    log.info("Entering update loop (5 min intervals)...")
    while not DISABLE_AUTOUPDATES:
        # Sleep for 5 minutes
        for i in range(0, 5):
            await try_websocket()
            time.sleep(60)

        # Capture temporary replacements after first 5 minutes
        if k == 0:
            new_data = await get_connected_atis_data()
            temp_rep = compare_atis_data(prev_data, new_data)

        log.info(f"Update cycle {k}")
        await configure_atises(connected_only=True, temp_rep=temp_rep)
        k += 1

if __name__ == "__main__":
    # Use first line for Desktop, second line for Jupyter
    asyncio.run(main())
    # await main()
