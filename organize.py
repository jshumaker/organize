#!/usr/bin/env python

import argparse
import yaml
import os
import os.path
from guessit import guessit
import re
import logging
import sys
from titlecase import titlecase
import transmissionrpc
import shutil
import subprocess
import copy
import sqlite3
import string
from pyxdameraulevenshtein import damerau_levenshtein_distance, normalized_damerau_levenshtein_distance, damerau_levenshtein_distance_withNPArray, normalized_damerau_levenshtein_distance_withNPArray
import numpy as np
import time
from singleton import SingleInstance

default_dir = os.path.join(os.getenv("HOME"), '.organize')
default_config = os.path.join(default_dir, 'config.yml')
default_log = os.path.join(default_dir, 'organize.log')
video_file_regex = '.*\.(mkv|mp4|avi|ogm|ts)$'

parser = argparse.ArgumentParser(description='Organize video downloads.')
parser.add_argument('--config', default=default_config, help='Configuration file, default ~/.organize/config.yml')
parser.add_argument('--logfile', default=default_log, help='Log file, default ~/.organize/organize.log')
parser.add_argument('--dryrun', action='store_true',
                    help="Don't perform any actions, instead report what would be done.")
parser.add_argument('--debug', action='store_true', help="Enable debug output.")
parser.add_argument('--cron', action='store_true', help="Disable all console output.")
parser.add_argument('--properclean', action='store_true',
                    help="Performs a proper/repack clean on the entire destinationfolder.")

args = parser.parse_args()

# set up logging to file - see previous section for more details
if args.debug:
    loglevel = logging.DEBUG
else:
    loglevel = logging.INFO

# Old Format: format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
    
logging.basicConfig(level=loglevel,
                    format='%(asctime)s %(levelname)-8s %(message)s',
                    datefmt='%m-%d %H:%M',
                    filename=args.logfile,
                    filemode='a')
if not args.cron:
    # define a Handler which writes to the sys.stdout
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(loglevel)
    # set a format which is simpler for console use
    formatter = logging.Formatter('%(levelname)-8s %(message)s')
    # tell the handler to use this format
    console.setFormatter(formatter)
    # add the handler to the root logger
    logging.getLogger('').addHandler(console)

# Prevent multiple copies
try:
    myinstance = SingleInstance()
except:
    sys.exit(0)


#Disable logging for guessit
logging.getLogger('guessit').setLevel(logging.CRITICAL)
logging.getLogger('GuessEpisodeInfoFromPosition').setLevel(logging.CRITICAL)
logging.getLogger('GuessFiletype').setLevel(logging.CRITICAL)
logging.getLogger('stevedore.extension').setLevel(logging.CRITICAL)
logging.getLogger('rebulk.rules').setLevel(logging.CRITICAL)
logging.getLogger('rebulk.rebulk').setLevel(logging.CRITICAL)
logging.getLogger('rebulk.processors').setLevel(logging.CRITICAL)


scriptdesc = "TV Torrent Organizer"
logging.debug('{0} starting.'.format(scriptdesc))

with open(args.config) as f:
    config_data = yaml.safe_load(f)
    

config_copy = copy.deepcopy(config_data)
config_copy['transmission']['password'] = '<redacted>'
logging.debug('Config file: {0}'.format(config_copy))


if 'overrides' in config_data.keys():
    overrides = config_data['overrides']
else:
    overrides = {}

# Open or initialize database.
database_file = os.path.join(os.getenv("HOME"), '.organize', 'copied.db')
db = sqlite3.connect(database_file)

# Create our table if it does not exist yet.
cursor = db.cursor()
cursor.execute('''
    create table if not exists copied (file TEXT)
''')
db.commit()

def db_add_copied(file):
    cursor = db.cursor()
    cursor.execute('INSERT INTO copied(file) VALUES (?)', (file,))
    db.commit()
    
def db_rem_copied(file):
    cursor = db.cursor()
    cursor.execute('DELETE FROM copied WHERE file = ?', (file,))
    db.commit()
    
def db_get_copied():
    cursor = db.cursor()
    cursor.execute('SELECT file FROM copied')
    return [file[0] for file in cursor]

def move_event(file, description):
    logging.debug('Checking for move event.')
    if 'events' in config_data.keys() and config_data['events'] is not None and 'moved' in config_data['events'] and config_data['events']['moved']:
        event_args = [config_data['events']['moved'], file, description]
        logging.info('Running move event: {0}'.format(" ".join(event_args)))
        try:
            p = subprocess.Popen(event_args, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT)
            eventoutput = p.communicate()[0]
            if p.returncode != 0:
                logging.error("Move event returned error {0}:\n{1}".format( eventoutput))
        except:
            logging.exception('Failed to execute move event {0}'.format(config_data['events']['move']))
        
client = None
retry_count = 0
while (client is None and retry_count < 5):
    retry_count += 1
    try:
        client = transmissionrpc.Client(config_data['transmission']['host'],port=config_data['transmission']['port'],user=config_data['transmission']['user'],password=config_data['transmission']['password'])
    except:
    	logging.exception("Failed to connect to transmission host, waiting 5 seconds and retrying")
        time.sleep(5)

if client is None:
    logging.error("Failed to connect to transmission")
    sys.exit(1)

# Cache a list of files that are seeding.
logging.debug('Creating cache of files from transmission.')
torrent_files = []
torrent_dirs = []
try:
    for torrent in client.get_torrents(arguments=['downloadDir', 'id', 'name']):
        directory = torrent.downloadDir
        dirwithname = os.path.join(directory, torrent.name)
        #logging.debug('Adding seeding directory: {0}'.format(dirwithname))
        torrent_dirs.append(dirwithname)
        for id, info in client.get_files(ids=[torrent.id])[torrent.id].iteritems():
            #logging.debug(os.path.join(directory,info['name']).encode('ascii', 'replace'))
            torrent_files.append(os.path.join(directory,info['name']))
except:
    logging.exception("Unable to build cache of seeding directories and files.")
    sys.exit(1)

        
        
def is_seeding(file):
    return file in torrent_files
def is_seeding_dir(dir):
    return dir in torrent_dirs

def find_files(directory, include, exclude=None):
    for root, dirs, files in os.walk(directory):
        for file in files:
            if re.match(include, file, re.IGNORECASE):
                if exclude is None or not re.match(exclude, file, re.IGNORECASE):
                    yield os.path.join(root, file)

def proper_cleanup(file):
    """
    Check if this file is a proper, and if so check if there's any matching files in the same folder that should be cleaned up.
    """
    logging.debug('Checking proper/repack {0} for replaced files to clean.'.format(file))
    if not re.match(r'.*\.(proper|repack)\..*\.(mkv|mp4|avi|ogm)$', file, re.IGNORECASE):
        logging.debug('Not a proper/repack')
        return
    # Check if this file has maybe been deleted by another repack/proper check already.
    if not os.path.exists(file):
        logging.debug('File does not exist')
        return
    
    directory = os.path.dirname(file)
    video_info = guessit(file)
    if not 'title' in video_info.keys() or not 'episode' in video_info.keys():
        logging.debug('Series and episode number undetermined, skipping proper/repack cleanup for {0}'.format(file))
        return
    matches = [file]
    for item in os.listdir(directory):
        matchfile = os.path.join(directory, item)
        if matchfile != file and os.path.isfile(matchfile) and re.match(video_file_regex, matchfile, re.IGNORECASE):
            video_info2 = guessit(matchfile)
            if (not 'season' in video_info.keys() or ('season' in video_info2.keys() and video_info['season'] == video_info2['season'])) and \
               (not 'screen_size' in video_info.keys() or ('screen_size' in video_info2.keys() and video_info['screen_size'] == video_info2['screen_size'])) and \
               'title' in video_info2.keys() and video_info['title'] == video_info2['title'] and \
               'episode' in video_info2.keys() and video_info['episode'] == video_info2['episode']:
                matches.append(matchfile)
    matches.sort(key=lambda x: os.path.getmtime(x), reverse=True)

    if len(matches) > 1:
        logging.info('Deleting files replaced by proper/repack: {0}'.format(matches[0]))
        for file in matches[1:]:
            if args.dryrun:
                logging.info('Would delete: {0}'.format(file))
            else:
                try:
                    os.remove(file)
                    logging.info('Deleted: {0}'.format(file))
                except:
                    logging.exception('Failed to delete {0}'.format(file))
    
video_files = []
   

# Iterate through the seeding directory, we should expect each of these to be a torrent, either a single file or a directory.
for item in sorted(os.listdir(config_data['directories']['seeding'])):
    path = os.path.join(config_data['directories']['seeding'], item)
    if os.path.isdir(path):
        # It's a directory, we need to check out what it contains.
        #logging.info('Searching for rar files in {0}'.format(path))
        rar_files = list(find_files(path, '.*\.rar$', '.*part(\d*[2-9]).rar$'))
        rar_files = [file for file in rar_files if (not re.search('\.subs\.', file, re.IGNORECASE)) and not os.path.exists(path + "/.autoextracted")]
        #rar_files = [file for file in rar_files if (not re.search('\.subs\.', file, re.IGNORECASE))]
        rar_files = [file for file in rar_files if (not re.search('\.sample\.', file, re.IGNORECASE))]
        
        video_files += list(find_files(path, video_file_regex))
        
        #print("{0} : {1} rar files, {2} video files".format(item, len(rar_files), len(video_files)))
        for rarfile in rar_files:
            if args.dryrun:
                logging.info("Would extract rar file: {0}".format(rarfile))
            else:
                try:
                    logging.info("Extracting rar file: {0}".format(rarfile))
                    command = ['unrar', 'x', '-o-', '-y', '-idq', rarfile, config_data['directories']['extracted']]
                    p = subprocess.Popen(command, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT)
                    raroutput = p.communicate()[0]
                    if p.returncode != 0:
                        logging.error("Failed to extract, command: {0} \nOutput:\n{1}".format(' '.join(command), raroutput))
                    else:
                        logging.info("Extracted rar file: {0}".format(rarfile))
                        open(os.path.join(path,'.autoextracted'), 'w').close()
                except:
                    logging.exception('Failed to extract {0}'.format(rarfile))
    elif re.match(video_file_regex, item, re.IGNORECASE):
        video_files.append(item)
    else:
        logging.info('Unrecognized item: {0}'.format(item))
        
video_files += find_files(config_data['directories']['extracted'], video_file_regex)
# Remove some unwanted files like sample files.
video_files = [file for file in video_files if \
    not re.search('/sample/', file, re.IGNORECASE) \
    and not re.search('[\.\-]sample\.', file, re.IGNORECASE)
    ]


def compare_strip(s):
    """
    Modify string for series comparison. Strips punctuation and sets to lowercase.
    :param s:
    :return:
    """
    # The following commented lines work in python 3 only.
    #table = string.maketrans("", "")
    #return s.lower().translate(table, string.punctuation)
    exclude = set(string.punctuation)
    s = ''.join(ch for ch in s if ch not in exclude)
    return s.lower()

# Get list of pre-existing series folders.
d = config_data['directories']['destination']
existing_series = [o for o in os.listdir(d) if os.path.isdir(os.path.join(d, o))]
existing_series_compare = np.array([compare_strip(o) for o in existing_series], dtype='S')

for file in video_files:
    try:
        base_filename = os.path.basename(file)
        video_info = guessit(base_filename)
        if not 'title' in video_info.keys():
            logging.warning('Unable to parse series name from: {}'.format(file))
            continue
        source_file = os.path.join(config_data['directories']['seeding'], file)
        series = titlecase(video_info['title'])


        # Check if there is a similar name we should use instead.
        distances = normalized_damerau_levenshtein_distance_withNPArray(compare_strip(series), existing_series_compare)
        #print(distances)
        min_distance = 1.0
        min_series = series
        for i in range(len(existing_series)):
            if distances[i] < min_distance:
                min_distance = distances[i]
                min_series = existing_series[i]
        logging.debug('Closest match({}): {} '.format(min_distance, min_series))
        if min_distance < 0.125:
            series = min_series

        destination = config_data['directories']['destination']
        # Check if there are overrides.
        for override in overrides:
            if re.match(override['match'], base_filename, re.IGNORECASE):
                if 'series' in override:
                    series = override['series']
                    logging.debug('Overriding series name to: {0}'.format(series))
                if 'destination' in override:
                    destination = override['destination']
                    logging.debug('Overriding destination folder to: {0}'.format(destination))

        if 'episode' in video_info.keys():
            episode_desc = "Episode {0}".format(video_info['episode'])
        else:
            # TODO: This is probably a special? Get some other details?
            episode_desc = "Special"
        if 'season' in video_info.keys():
            target_dir = os.path.join(destination, series, 'Season {0}'.format(video_info['season'])) + os.sep
            description = '{0} - Season {1} - {2}'.format(series, video_info['season'], episode_desc)
        else:
            target_dir = os.path.join(destination, series) + os.sep
            description = '{0} - {1}'.format(series, episode_desc)
        target_file = os.path.join(target_dir, os.path.basename(file))

        if os.path.exists(target_file) and os.path.getsize(target_file) > os.path.getsize(source_file):
            logging.error('Target file already exists and is larger, {0}'.format(target_file))
            continue

        if not os.path.exists(target_dir) and not args.dryrun:
            os.makedirs(target_dir)
        if is_seeding(source_file):
            if source_file in db_get_copied():
                logging.debug('Ignoring file {0}, it has already been copied.'.format(source_file))
            elif args.dryrun:
                logging.info('Would copy and schedule original for delete {0} to {1}'.format(source_file, target_dir))
            else:
                # Copy the file and record in some sort of db the later removal.
                logging.info('Copying and schedule original for delete: {0} to {1}'.format(source_file, target_dir))
                try:
                    db_add_copied(source_file)
                    shutil.copy(source_file, target_dir)
                    move_event(target_file, description)
                    proper_cleanup(target_file)
                except:
                    logging.exception('Failed to copy file.')
        elif source_file in db_get_copied():
            if args.dryrun:
                logging.info('Would delete already copied file {0}'.format(source_file))
            else:
                logging.info('Deleting already moved file {0}'.format(source_file))
                try:
                    os.remove(source_file)
                except:
                    logging.exception('Failed to delete file.')
        else:
            if args.dryrun:
                logging.info('Would move {0} to {1}'.format(source_file, target_dir))
            else:
                logging.info('Moving {0} to {1}'.format(source_file, target_dir))
                try:
                    # Delete any pre-existing files in the way. Default is to replace, check happens earlier to make sure we're not replacing with an incomplete file.
                    if os.path.exists(target_file):
                        os.remove(target_file)
                    shutil.move(source_file, target_dir)
                    move_event(target_file, description)
                    proper_cleanup(target_file)
                except IOError as e:
                    if e.errno == 13:
                        logging.warn('Invalid permissions to move file.')
                    else:
                        logging.exception('Failed to move file.')
                except:
                    logging.exception('Failed to move file.')
    except:
        logging.exception('Failed to process {0}'.format(file))
                
# Clean up seeding folder of auto extracted files that are no longer seeding.
for item in sorted(os.listdir(config_data['directories']['seeding'])):
    path = os.path.join(config_data['directories']['seeding'], item)
    if os.path.isdir(path) and os.path.exists(os.path.join(path, '.autoextracted')) and not is_seeding_dir(path):
        if args.dryrun:
            logging.info('Would delete auto extracted torrent directory: {0}'.format(path))
        else:
            try:
                shutil.rmtree(path)
                logging.info('Deleted previously extracted folder: {0}'.format(path))
            except:
                logging.exception('Failed to delete previously extracted folder: {0}'.format(path))


# Remove complete torrents, cleanup files left behind.
#seeding_limit = datetime.timedelta(days=28)
for torrent in client.get_torrents():
    # Only process files from our seeding directory.
    if not torrent.downloadDir == config_data['directories']['seeding']:
        continue
        
    completed = False
    if torrent.status == 'stopped' and torrent.progress == 100:
        completed = True
    #elif torrent.status == 'seeding' and torrent.progress == 100 and (datetime.datetime.now() - torrent.date_done) > seeding_limit:
    #    completed = True
    
    if completed:
        if args.dryrun:
            logging.info('Would remove torrent: {0}'.format(torrent.name))
        else:
            logging.info('Removing completed torrent: {0}'.format(torrent.name))
            client.remove_torrent(torrent.hashString, delete_data=False)
    
        torrent_path = os.path.join(torrent.downloadDir, torrent.name)
        
        if os.path.exists(os.path.join(torrent_path, '.autoextracted')):
            if args.dryrun:
                logging.info('Would delete previously extracted: {0}'.format(torrent_path))
            else:
                logging.info('Deleting previously extracted: {0}'.format(torrent_path))
                shutil.rmtree(torrent_path)
        elif os.path.join(torrent.downloadDir, torrent.name) in db_get_copied():
            if args.dryrun:
                logging.info('Would delete previously copied file: {0}'.format(torrent_path))
            else:
                logging.info('Deleting previously copied file: {0}'.format(torrent_path))
                os.remove(torrent_path)
    # Following supports moving completed files to a completed folder, disabled for now.
    #else:
    #    print('Moving %s from seeding to complete' % torrent.name)
    #    if dirmatch:
    #      shutil.move(os.path.join(torrent.downloadDir, torrent.name), os.path.join(complete_dir + '/' + dirname, torrent.name))
    #    else:
    #      shutil.move(os.path.join(torrent.downloadDir, torrent.name), os.path.join(complete_dir, torrent.name))


# Clean up copied files that are no longer seeding.
for file in db_get_copied():
    if not is_seeding(file):
        if args.dryrun:
            logging.info('Would delete previously copied file: {0}'.format(file))
        else:
            try:
                if os.path.exists(file):
                    os.remove(file)
                    logging.info('Deleted previously copied file: {0}'.format(file))
                else:
                    logging.warn("Previously copied file didn't exist, unable to delete: {0}".format(file))
                db_rem_copied(file)
            except:
                logging.exception('Failed to delete previously copied file: {0}'.format(file))

# TODO: Clean up files for torrents that were possible manually removed from transmission.


# Perform global clean up of propers and repacks by deleting the files they are replacing.
if args.properclean:
    for file in find_files(config_data['directories']['destination'], r'.*\.(proper|repack)\..*\.(mkv|mp4|avi|ogm)$'):
        proper_cleanup(file)
    
logging.debug('{0} finished.'.format(scriptdesc))
