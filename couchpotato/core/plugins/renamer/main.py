from couchpotato import get_session
from couchpotato.api import addApiView
from couchpotato.core.event import addEvent, fireEvent, fireEventAsync
from couchpotato.core.helpers.encoding import toUnicode, ss
from couchpotato.core.helpers.variable import getExt, mergeDicts, getTitle, \
    getImdb, link, symlink, tryInt
from couchpotato.core.logger import CPLog
from couchpotato.core.plugins.base import Plugin
from couchpotato.core.settings.model import Library, File, Profile, Release, \
    ReleaseInfo
from couchpotato.environment import Env
from unrar2 import RarFile
import errno
import fnmatch
import os
import re
import shutil
import time
import traceback

log = CPLog(__name__)

class Renamer(Plugin):

    renaming_started = False
    checking_snatched = False

    def __init__(self):
        addApiView('renamer.scan', self.scanView, docs = {
            'desc': 'For the renamer to check for new files to rename in a folder',
            'params': {
                'async': {'desc': 'Optional: Set to 1 if you dont want to fire the renamer.scan asynchronous.'},
                'movie_folder': {'desc': 'Optional: The folder of the movie to scan. Keep empty for default renamer folder.'},
                'downloader' : {'desc': 'Optional: The downloader this movie has been downloaded with'},
                'download_id': {'desc': 'Optional: The downloader\'s nzb/torrent ID'},
            },
        })

        addEvent('renamer.scan', self.scan)
        addEvent('renamer.check_snatched', self.checkSnatched)

        addEvent('app.load', self.scan)
        addEvent('app.load', self.setCrons)

        # Enable / disable interval
        addEvent('setting.save.renamer.enabled.after', self.setCrons)
        addEvent('setting.save.renamer.run_every.after', self.setCrons)
        addEvent('setting.save.renamer.force_every.after', self.setCrons)

    def setCrons(self):

        fireEvent('schedule.remove', 'renamer.check_snatched')
        if self.isEnabled() and self.conf('run_every') > 0:
            fireEvent('schedule.interval', 'renamer.check_snatched', self.checkSnatched, minutes = self.conf('run_every'), single = True)

        fireEvent('schedule.remove', 'renamer.check_snatched_forced')
        if self.isEnabled() and self.conf('force_every') > 0:
            fireEvent('schedule.interval', 'renamer.check_snatched_forced', self.scan, hours = self.conf('force_every'), single = True)

        return True

    def scanView(self, **kwargs):

        async = tryInt(kwargs.get('async', 0))
        movie_folder = kwargs.get('movie_folder')
        downloader = kwargs.get('downloader')
        download_id = kwargs.get('download_id')

        download_info = {'folder': movie_folder} if movie_folder else None
        if download_info:
            download_info.update({'id': download_id, 'downloader': downloader} if download_id else {})

        fire_handle = fireEvent if not async else fireEventAsync

        fire_handle('renamer.scan', download_info)

        return {
            'success': True
        }

    def scan(self, download_info = None):

        if self.isDisabled():
            return

        if self.renaming_started is True:
            log.info('Renamer is already running, if you see this often, check the logs above for errors.')
            return

        movie_folder = download_info and download_info.get('folder')

        # Check to see if the "to" folder is inside the "from" folder.
        if movie_folder and not os.path.isdir(movie_folder) or not os.path.isdir(self.conf('from')) or not os.path.isdir(self.conf('to')):
            l = log.debug if movie_folder else log.error
            l('Both the "To" and "From" have to exist.')
            return
        elif self.conf('from') in self.conf('to'):
            log.error('The "to" can\'t be inside of the "from" folder. You\'ll get an infinite loop.')
            return
        elif movie_folder and movie_folder in [self.conf('to'), self.conf('from')]:
            log.error('The "to" and "from" folders can\'t be inside of or the same as the provided movie folder.')
            return

        # Make sure a checkSnatched marked all downloads/seeds as such
        if not download_info and self.conf('run_every') > 0:
            fireEvent('renamer.check_snatched')

        self.renaming_started = True

        # make sure the movie folder name is included in the search
        folder = None
        files = []
        if movie_folder:
            log.info('Scanning movie folder %s...', movie_folder)
            movie_folder = movie_folder.rstrip(os.path.sep)
            folder = os.path.dirname(movie_folder)

            # Get all files from the specified folder
            try:
                for root, folders, names in os.walk(movie_folder):
                    files.extend([os.path.join(root, name) for name in names])
            except:
                log.error('Failed getting files from %s: %s', (movie_folder, traceback.format_exc()))

        db = get_session()

        # Extend the download info with info stored in the downloaded release
        download_info = self.extendDownloadInfo(download_info)

        # Unpack any archives
        extr_files = None
        if self.conf('unrar'):
            folder, movie_folder, files, extr_files = self.extractFiles(folder = folder, movie_folder = movie_folder, files = files,
                                                                        cleanup = self.conf('cleanup') and not self.downloadIsTorrent(download_info))

        groups = fireEvent('scanner.scan', folder = folder if folder else self.conf('from'),
                           files = files, download_info = download_info, return_ignored = False, single = True)

        folder_name = self.conf('folder_name')
        file_name = self.conf('file_name')
        trailer_name = self.conf('trailer_name')
        nfo_name = self.conf('nfo_name')
        separator = self.conf('separator')

        # Statusses
        done_status, active_status, downloaded_status, snatched_status = \
            fireEvent('status.get', ['done', 'active', 'downloaded', 'snatched'], single = True)

        for group_identifier in groups:

            group = groups[group_identifier]
            rename_files = {}
            remove_files = []
            remove_releases = []

            movie_title = getTitle(group['library'])

            # Add _UNKNOWN_ if no library item is connected
            if not group['library'] or not movie_title:
                self.tagDir(group, 'unknown')
                continue
            # Rename the files using the library data
            else:
                group['library'] = fireEvent('library.update.movie', identifier = group['library']['identifier'], single = True)
                if not group['library']:
                    log.error('Could not rename, no library item to work with: %s', group_identifier)
                    continue

                library = group['library']
                library_ent = db.query(Library).filter_by(identifier = group['library']['identifier']).first()

                movie_title = getTitle(library)

                # Overwrite destination when set in category
                destination = self.conf('to')
                for movie in library_ent.movies:
                    if movie.category and movie.category.destination and len(movie.category.destination) > 0 and movie.category.destination != 'None':
                        destination = movie.category.destination
                        log.debug('Setting category destination for "%s": %s' % (movie_title, destination))
                    else:
                        log.debug('No category destination found for "%s"' % movie_title)

                    break

                # Find subtitle for renaming
                group['before_rename'] = []
                fireEvent('renamer.before', group)

                # Add extracted files to the before_rename list
                if extr_files:
                    group['before_rename'].extend(extr_files)

                # Remove weird chars from moviename
                movie_name = re.sub(r"[\x00\/\\:\*\?\"<>\|]", '', movie_title)

                # Put 'The' at the end
                name_the = movie_name
                if movie_name[:4].lower() == 'the ':
                    name_the = movie_name[4:] + ', The'

                replacements = {
                     'ext': 'mkv',
                     'namethe': name_the.strip(),
                     'thename': movie_name.strip(),
                     'year': library['year'],
                     'first': name_the[0].upper(),
                     'quality': group['meta_data']['quality']['label'],
                     'quality_type': group['meta_data']['quality_type'],
                     'video': group['meta_data'].get('video'),
                     'audio': group['meta_data'].get('audio'),
                     'group': group['meta_data']['group'],
                     'source': group['meta_data']['source'],
                     'resolution_width': group['meta_data'].get('resolution_width'),
                     'resolution_height': group['meta_data'].get('resolution_height'),
                     'audio_channels': group['meta_data'].get('audio_channels'),
                     'imdb_id': library['identifier'],
                     'cd': '',
                     'cd_nr': '',
                     'mpaa': library['info'].get('mpaa', ''),
                }

                for file_type in group['files']:

                    # Move nfo depending on settings
                    if file_type is 'nfo' and not self.conf('rename_nfo'):
                        log.debug('Skipping, renaming of %s disabled', file_type)
                        for current_file in group['files'][file_type]:
                            if self.conf('cleanup') and (not self.downloadIsTorrent(download_info) or self.fileIsAdded(current_file, group)):
                                remove_files.append(current_file)
                        continue

                    # Subtitle extra
                    if file_type is 'subtitle_extra':
                        continue

                    # Move other files
                    multiple = len(group['files'][file_type]) > 1 and not group['is_dvd']
                    cd = 1 if multiple else 0

                    for current_file in sorted(list(group['files'][file_type])):
                        current_file = toUnicode(current_file)

                        # Original filename
                        replacements['original'] = os.path.splitext(os.path.basename(current_file))[0]
                        replacements['original_folder'] = fireEvent('scanner.remove_cptag', group['dirname'], single = True)

                        # Extension
                        replacements['ext'] = getExt(current_file)

                        # cd #
                        replacements['cd'] = ' cd%d' % cd if multiple else ''
                        replacements['cd_nr'] = cd if multiple else ''

                        # Naming
                        final_folder_name = self.doReplace(folder_name, replacements, folder = True)
                        final_file_name = self.doReplace(file_name, replacements)
                        replacements['filename'] = final_file_name[:-(len(getExt(final_file_name)) + 1)]

                        # Meta naming
                        if file_type is 'trailer':
                            final_file_name = self.doReplace(trailer_name, replacements, remove_multiple = True)
                        elif file_type is 'nfo':
                            final_file_name = self.doReplace(nfo_name, replacements, remove_multiple = True)

                        # Seperator replace
                        if separator:
                            final_file_name = final_file_name.replace(' ', separator)

                        # Move DVD files (no structure renaming)
                        if group['is_dvd'] and file_type is 'movie':
                            found = False
                            for top_dir in ['video_ts', 'audio_ts', 'bdmv', 'certificate']:
                                has_string = current_file.lower().find(os.path.sep + top_dir + os.path.sep)
                                if has_string >= 0:
                                    structure_dir = current_file[has_string:].lstrip(os.path.sep)
                                    rename_files[current_file] = os.path.join(destination, final_folder_name, structure_dir)
                                    found = True
                                    break

                            if not found:
                                log.error('Could not determine dvd structure for: %s', current_file)

                        # Do rename others
                        else:
                            if file_type is 'leftover':
                                if self.conf('move_leftover'):
                                    rename_files[current_file] = os.path.join(destination, final_folder_name, os.path.basename(current_file))
                            elif file_type not in ['subtitle']:
                                rename_files[current_file] = os.path.join(destination, final_folder_name, final_file_name)

                        # Check for extra subtitle files
                        if file_type is 'subtitle':

                            remove_multiple = False
                            if len(group['files']['movie']) == 1:
                                remove_multiple = True

                            sub_langs = group['subtitle_language'].get(current_file, [])

                            # rename subtitles with or without language
                            sub_name = self.doReplace(file_name, replacements, remove_multiple = remove_multiple)
                            rename_files[current_file] = os.path.join(destination, final_folder_name, sub_name)

                            rename_extras = self.getRenameExtras(
                                extra_type = 'subtitle_extra',
                                replacements = replacements,
                                folder_name = folder_name,
                                file_name = file_name,
                                destination = destination,
                                group = group,
                                current_file = current_file,
                                remove_multiple = remove_multiple,
                            )

                            # Don't add language if multiple languages in 1 subtitle file
                            if len(sub_langs) == 1:
                                sub_name = sub_name.replace(replacements['ext'], '%s.%s' % (sub_langs[0], replacements['ext']))
                                rename_files[current_file] = os.path.join(destination, final_folder_name, sub_name)

                            rename_files = mergeDicts(rename_files, rename_extras)

                        # Filename without cd etc
                        elif file_type is 'movie':
                            rename_extras = self.getRenameExtras(
                                extra_type = 'movie_extra',
                                replacements = replacements,
                                folder_name = folder_name,
                                file_name = file_name,
                                destination = destination,
                                group = group,
                                current_file = current_file
                            )
                            rename_files = mergeDicts(rename_files, rename_extras)

                            group['filename'] = self.doReplace(file_name, replacements, remove_multiple = True)[:-(len(getExt(final_file_name)) + 1)]
                            group['destination_dir'] = os.path.join(destination, final_folder_name)

                        if multiple:
                            cd += 1

                # Before renaming, remove the lower quality files
                remove_leftovers = True

                # Add it to the wanted list before we continue
                if len(library_ent.movies) == 0:
                    profile = db.query(Profile).filter_by(core = True, label = group['meta_data']['quality']['label']).first()
                    fireEvent('movie.add', params = {'identifier': group['library']['identifier'], 'profile_id': profile.id}, search_after = False)
                    db.expire_all()
                    library_ent = db.query(Library).filter_by(identifier = group['library']['identifier']).first()

                for movie in library_ent.movies:

                    # Mark movie "done" once it's found the quality with the finish check
                    try:
                        if movie.status_id == active_status.get('id') and movie.profile:
                            for profile_type in movie.profile.types:
                                if profile_type.quality_id == group['meta_data']['quality']['id'] and profile_type.finish:
                                    movie.status_id = done_status.get('id')
                                    movie.last_edit = int(time.time())
                                    db.commit()
                    except Exception, e:
                        log.error('Failed marking movie finished: %s %s', (e, traceback.format_exc()))

                    # Go over current movie releases
                    for release in movie.releases:

                        # When a release already exists
                        if release.status_id is done_status.get('id'):

                            # This is where CP removes older, lesser quality releases
                            if release.quality.order > group['meta_data']['quality']['order']:
                                log.info('Removing lesser quality %s for %s.', (movie.library.titles[0].title, release.quality.label))
                                for current_file in release.files:
                                    remove_files.append(current_file)
                                remove_releases.append(release)
                            # Same quality, but still downloaded, so maybe repack/proper/unrated/directors cut etc
                            elif release.quality.order is group['meta_data']['quality']['order']:
                                log.info('Same quality release already exists for %s, with quality %s. Assuming repack.', (movie.library.titles[0].title, release.quality.label))
                                for current_file in release.files:
                                    remove_files.append(current_file)
                                remove_releases.append(release)

                            # Downloaded a lower quality, rename the newly downloaded files/folder to exclude them from scan
                            else:
                                log.info('Better quality release already exists for %s, with quality %s', (movie.library.titles[0].title, release.quality.label))

                                # Add exists tag to the .ignore file
                                self.tagDir(group, 'exists')

                                # Notify on rename fail
                                download_message = 'Renaming of %s (%s) cancelled, exists in %s already.' % (movie.library.titles[0].title, group['meta_data']['quality']['label'], release.quality.label)
                                fireEvent('movie.renaming.canceled', message = download_message, data = group)
                                remove_leftovers = False

                                break
                        elif release.status_id is snatched_status.get('id'):
                            if release.quality.id is group['meta_data']['quality']['id']:
                                log.debug('Marking release as downloaded')
                                try:
                                    release.status_id = downloaded_status.get('id')
                                    release.last_edit = int(time.time())
                                except Exception, e:
                                    log.error('Failed marking release as finished: %s %s', (e, traceback.format_exc()))

                                db.commit()

                # Remove leftover files
                if not remove_leftovers: # Don't remove anything
                    break

                log.debug('Removing leftover files')
                for current_file in group['files']['leftover']:
                    if self.conf('cleanup') and not self.conf('move_leftover') and \
                            (not self.downloadIsTorrent(download_info) or self.fileIsAdded(current_file, group)):
                        remove_files.append(current_file)

            # Remove files
            delete_folders = []
            for src in remove_files:

                if isinstance(src, File):
                    src = src.path

                if rename_files.get(src):
                    log.debug('Not removing file that will be renamed: %s', src)
                    continue

                log.info('Removing "%s"', src)
                try:
                    src = ss(src)
                    if os.path.isfile(src):
                        os.remove(src)

                        parent_dir = os.path.normpath(os.path.dirname(src))
                        if delete_folders.count(parent_dir) == 0 and os.path.isdir(parent_dir) and not parent_dir in [destination, movie_folder] and not self.conf('from') in parent_dir:
                            delete_folders.append(parent_dir)

                except:
                    log.error('Failed removing %s: %s', (src, traceback.format_exc()))
                    self.tagDir(group, 'failed_remove')

            # Delete leftover folder from older releases
            for delete_folder in delete_folders:
                try:
                    self.deleteEmptyFolder(delete_folder, show_error = False)
                except Exception, e:
                    log.error('Failed to delete folder: %s %s', (e, traceback.format_exc()))

            # Rename all files marked
            group['renamed_files'] = []
            for src in rename_files:
                if rename_files[src]:
                    dst = rename_files[src]
                    log.info('Renaming "%s" to "%s"', (src, dst))

                    # Create dir
                    self.makeDir(os.path.dirname(dst))

                    try:
                        self.moveFile(src, dst, forcemove = not self.downloadIsTorrent(download_info) or self.fileIsAdded(src, group))
                        group['renamed_files'].append(dst)
                    except:
                        log.error('Failed moving the file "%s" : %s', (os.path.basename(src), traceback.format_exc()))
                        self.tagDir(group, 'failed_rename')

            # Tag folder if it is in the 'from' folder and it will not be removed because it is a torrent
            if self.movieInFromFolder(movie_folder) and self.downloadIsTorrent(download_info):
                self.tagDir(group, 'renamed_already')

            # Remove matching releases
            for release in remove_releases:
                log.debug('Removing release %s', release.identifier)
                try:
                    db.delete(release)
                except:
                    log.error('Failed removing %s: %s', (release.identifier, traceback.format_exc()))

            if group['dirname'] and group['parentdir'] and not self.downloadIsTorrent(download_info):
                try:
                    log.info('Deleting folder: %s', group['parentdir'])
                    self.deleteEmptyFolder(group['parentdir'])
                except:
                    log.error('Failed removing %s: %s', (group['parentdir'], traceback.format_exc()))

            # Notify on download, search for trailers etc
            download_message = 'Downloaded %s (%s)' % (movie_title, replacements['quality'])
            try:
                fireEvent('renamer.after', message = download_message, group = group, in_order = True)
            except:
                log.error('Failed firing (some) of the renamer.after events: %s', traceback.format_exc())

            # Break if CP wants to shut down
            if self.shuttingDown():
                break

        self.renaming_started = False

    def getRenameExtras(self, extra_type = '', replacements = None, folder_name = '', file_name = '', destination = '', group = None, current_file = '', remove_multiple = False):
        if not group: group = {}
        if not replacements: replacements = {}

        replacements = replacements.copy()
        rename_files = {}

        def test(s):
            return current_file[:-len(replacements['ext'])] in s

        for extra in set(filter(test, group['files'][extra_type])):
            replacements['ext'] = getExt(extra)

            final_folder_name = self.doReplace(folder_name, replacements, remove_multiple = remove_multiple, folder = True)
            final_file_name = self.doReplace(file_name, replacements, remove_multiple = remove_multiple)
            rename_files[extra] = os.path.join(destination, final_folder_name, final_file_name)

        return rename_files

    # This adds a file to ignore / tag a release so it is ignored later
    def tagDir(self, group, tag):

        ignore_file = None
        if isinstance(group, dict):
            for movie_file in sorted(list(group['files']['movie'])):
                ignore_file = '%s.%s.ignore' % (os.path.splitext(movie_file)[0], tag)
                break
        else:
            if not os.path.isdir(group) or not tag:
                return
            ignore_file = os.path.join(group, '%s.ignore' % tag)


        text = """This file is from CouchPotato
It has marked this release as "%s"
This file hides the release from the renamer
Remove it if you want it to be renamed (again, or at least let it try again)
""" % tag

        if ignore_file:
            self.createFile(ignore_file, text)

    def untagDir(self, folder, tag = ''):
        if not os.path.isdir(folder):
            return

        # Remove any .ignore files
        for root, dirnames, filenames in os.walk(folder):
            for filename in fnmatch.filter(filenames, '*%s.ignore' % tag):
                os.remove((os.path.join(root, filename)))

    def hastagDir(self, folder, tag = ''):
        if not os.path.isdir(folder):
            return False

        # Find any .ignore files
        for root, dirnames, filenames in os.walk(folder):
            if fnmatch.filter(filenames, '*%s.ignore' % tag):
                return True

        return False

    def moveFile(self, old, dest, forcemove = False):
        dest = ss(dest)
        try:
            if forcemove:
                shutil.move(old, dest)
            elif self.conf('file_action') == 'copy':
                shutil.copy(old, dest)
            elif self.conf('file_action') == 'link':
                # First try to hardlink
                try:
                    log.debug('Hardlinking file "%s" to "%s"...', (old, dest))
                    link(old, dest)
                except:
                    # Try to simlink next
                    log.debug('Couldn\'t hardlink file "%s" to "%s". Simlinking instead. Error: %s. ', (old, dest, traceback.format_exc()))
                    shutil.copy(old, dest)
                    try:
                        symlink(dest, old + '.link')
                        os.unlink(old)
                        os.rename(old + '.link', old)
                    except:
                        log.error('Couldn\'t symlink file "%s" to "%s". Copied instead. Error: %s. ', (old, dest, traceback.format_exc()))
            else:
                shutil.move(old, dest)

            try:
                os.chmod(dest, Env.getPermission('file'))
                if os.name == 'nt' and self.conf('ntfs_permission'):
                    os.popen('icacls "' + dest + '"* /reset /T')
            except:
                log.error('Failed setting permissions for file: %s, %s', (dest, traceback.format_exc(1)))

        except OSError, err:
            # Copying from a filesystem with octal permission to an NTFS file system causes a permission error.  In this case ignore it.
            if not hasattr(os, 'chmod') or err.errno != errno.EPERM:
                raise
            else:
                if os.path.exists(dest):
                    os.unlink(old)

        except:
            log.error('Couldn\'t move file "%s" to "%s": %s', (old, dest, traceback.format_exc()))
            raise

        return True

    def doReplace(self, string, replacements, remove_multiple = False, folder = False):
        """
        replace confignames with the real thing
        """

        replacements = replacements.copy()
        if remove_multiple:
            replacements['cd'] = ''
            replacements['cd_nr'] = ''

        replaced = toUnicode(string)
        for x, r in replacements.iteritems():
            if r is not None:
                replaced = replaced.replace(u'<%s>' % toUnicode(x), toUnicode(r))
            else:
                #If information is not available, we don't want the tag in the filename
                replaced = replaced.replace('<' + x + '>', '')

        replaced = re.sub(r"[\x00:\*\?\"<>\|]", '', replaced)

        sep = self.conf('foldersep') if folder else self.conf('separator')
        return self.replaceDoubles(replaced.lstrip('. ')).replace(' ', ' ' if not sep else sep)

    def replaceDoubles(self, string):
        return string.replace('  ', ' ').replace(' .', '.')

    def deleteEmptyFolder(self, folder, show_error = True):
        folder = ss(folder)

        loge = log.error if show_error else log.debug
        for root, dirs, files in os.walk(folder):

            for dir_name in dirs:
                full_path = os.path.join(root, dir_name)
                if len(os.listdir(full_path)) == 0:
                    try:
                        os.rmdir(full_path)
                    except:
                        loge('Couldn\'t remove empty directory %s: %s', (full_path, traceback.format_exc()))

        try:
            os.rmdir(folder)
        except:
            loge('Couldn\'t remove empty directory %s: %s', (folder, traceback.format_exc()))

    def checkSnatched(self):

        if self.checking_snatched:
            log.debug('Already checking snatched')
            return False

        self.checking_snatched = True

        snatched_status, ignored_status, failed_status, done_status, seeding_status, downloaded_status = \
            fireEvent('status.get', ['snatched', 'ignored', 'failed', 'done', 'seeding', 'downloaded'], single = True)

        db = get_session()
        rels = db.query(Release).filter_by(status_id = snatched_status.get('id')).all()
        rels.extend(db.query(Release).filter_by(status_id = seeding_status.get('id')).all())

        scan_items = []
        scan_required = False

        if rels:
            log.debug('Checking status snatched releases...')

            statuses = fireEvent('download.status', merge = True)
            if not statuses:
                log.debug('Download status functionality is not implemented for active downloaders.')
                scan_required = True
            else:
                try:
                    for rel in rels:
                        rel_dict = rel.to_dict({'info': {}})

                        movie_dict = fireEvent('movie.get', rel.movie_id, single = True)

                        # check status
                        nzbname = self.createNzbName(rel_dict['info'], movie_dict)

                        found = False
                        for item in statuses:
                            found_release = False
                            if rel_dict['info'].get('download_id'):
                                if item['id'] == rel_dict['info']['download_id'] and item['downloader'] == rel_dict['info']['download_downloader']:
                                    log.debug('Found release by id: %s', item['id'])
                                    found_release = True
                            else:
                                if item['name'] == nzbname or rel_dict['info']['name'] in item['name'] or getImdb(item['name']) == movie_dict['library']['identifier']:
                                    found_release = True

                            if found_release:
                                timeleft = 'N/A' if item['timeleft'] == -1 else item['timeleft']
                                log.debug('Found %s: %s, time to go: %s', (item['name'], item['status'].upper(), timeleft))

                                if item['status'] == 'busy':
                                    # Tag folder if it is in the 'from' folder and it will not be processed because it is still downloading
                                    if item['folder'] and self.conf('from') in item['folder']:
                                        self.tagDir(item['folder'], 'downloading')

                                elif item['status'] == 'seeding':

                                    #If linking setting is enabled, process release
                                    if self.conf('file_action') != 'move' and not rel.movie.status_id == done_status.get('id') and self.statusInfoComplete(item):
                                        log.info('Download of %s completed! It is now being processed while leaving the original files alone for seeding. Current ratio: %s.', (item['name'], item['seed_ratio']))

                                        # Remove the downloading tag
                                        self.untagDir(item['folder'], 'downloading')

                                        rel.status_id = seeding_status.get('id')
                                        rel.last_edit = int(time.time())
                                        db.commit()

                                        # Scan and set the torrent to paused if required
                                        item.update({'pause': True, 'scan': True, 'process_complete': False})
                                        scan_items.append(item)
                                    else:
                                        if rel.status_id != seeding_status.get('id'):
                                            rel.status_id = seeding_status.get('id')
                                            rel.last_edit = int(time.time())
                                            db.commit()

                                        #let it seed
                                        log.debug('%s is seeding with ratio: %s', (item['name'], item['seed_ratio']))
                                elif item['status'] == 'failed':
                                    fireEvent('download.remove_failed', item, single = True)
                                    rel.status_id = failed_status.get('id')
                                    rel.last_edit = int(time.time())
                                    db.commit()

                                    if self.conf('next_on_failed'):
                                        fireEvent('movie.searcher.try_next_release', movie_id = rel.movie_id)
                                elif item['status'] == 'completed':
                                    log.info('Download of %s completed!', item['name'])
                                    if self.statusInfoComplete(item):

                                        # If the release has been seeding, process now the seeding is done
                                        if rel.status_id == seeding_status.get('id'):
                                            if rel.movie.status_id == done_status.get('id'):
                                                # Set the release to done as the movie has already been renamed
                                                rel.status_id = downloaded_status.get('id')
                                                rel.last_edit = int(time.time())
                                                db.commit()

                                                # Allow the downloader to clean-up
                                                item.update({'pause': False, 'scan': False, 'process_complete': True})
                                                scan_items.append(item)
                                            else:
                                                # Set the release to snatched so that the renamer can process the release as if it was never seeding
                                                rel.status_id = snatched_status.get('id')
                                                rel.last_edit = int(time.time())
                                                db.commit()

                                                # Scan and Allow the downloader to clean-up
                                                item.update({'pause': False, 'scan': True, 'process_complete': True})
                                                scan_items.append(item)

                                        else:
                                            # Remove the downloading tag
                                            self.untagDir(item['folder'], 'downloading')

                                            # Scan and Allow the downloader to clean-up
                                            item.update({'pause': False, 'scan': True, 'process_complete': True})
                                            scan_items.append(item)
                                    else:
                                        scan_required = True

                                found = True
                                break

                        if not found:
                            log.info('%s not found in downloaders', nzbname)

                except:
                    log.error('Failed checking for release in downloader: %s', traceback.format_exc())

        # The following can either be done here, or inside the scanner if we pass it scan_items in one go
        for item in scan_items:
            # Ask the renamer to scan the item
            if item['scan']:
                if item['pause'] and self.conf('file_action') == 'link':
                    fireEvent('download.pause', item = item, pause = True, single = True)
                fireEvent('renamer.scan', download_info = item)
                if item['pause'] and self.conf('file_action') == 'link':
                    fireEvent('download.pause', item = item, pause = False, single = True)
            if item['process_complete']:
                #First make sure the files were succesfully processed
                if not self.hastagDir(item['folder'], 'failed_rename'):
                    # Remove the seeding tag if it exists
                    self.untagDir(item['folder'], 'renamed_already')
                    # Ask the downloader to process the item
                    fireEvent('download.process_complete', item = item, single = True)

        if scan_required:
            fireEvent('renamer.scan')

        self.checking_snatched = False

        return True

    def extendDownloadInfo(self, download_info):

        rls = None

        if download_info and download_info.get('id') and download_info.get('downloader'):

            db = get_session()

            rlsnfo_dwnlds = db.query(ReleaseInfo).filter_by(identifier = 'download_downloader', value = download_info.get('downloader')).all()
            rlsnfo_ids = db.query(ReleaseInfo).filter_by(identifier = 'download_id', value = download_info.get('id')).all()

            for rlsnfo_dwnld in rlsnfo_dwnlds:
                for rlsnfo_id in rlsnfo_ids:
                    if rlsnfo_id.release == rlsnfo_dwnld.release:
                        rls = rlsnfo_id.release
                        break
                if rls: break

            if not rls:
                log.error('Download ID %s from downloader %s not found in releases', (download_info.get('id'), download_info.get('downloader')))

        if rls:

            rls_dict = rls.to_dict({'info':{}})
            download_info.update({
                'imdb_id': rls.movie.library.identifier,
                'quality': rls.quality.identifier,
                'protocol': rls_dict.get('info', {}).get('protocol') or rls_dict.get('info', {}).get('type'),
            })

        return download_info

    def downloadIsTorrent(self, download_info):
        return download_info and download_info.get('protocol') in ['torrent', 'torrent_magnet']

    def fileIsAdded(self, src, group):
        if not group or not group.get('before_rename'):
            return False
        return src in group['before_rename']

    def statusInfoComplete(self, item):
        return item['id'] and item['downloader'] and item['folder']

    def movieInFromFolder(self, movie_folder):
        return movie_folder and self.conf('from') in movie_folder or not movie_folder

    def extractFiles(self, folder = None, movie_folder = None, files = None, cleanup = False):
        if not files: files = []

        # RegEx for finding rar files
        archive_regex = '(?P<file>^(?P<base>(?:(?!\.part\d+\.rar$).)*)\.(?:(?:part0*1\.)?rar)$)'
        restfile_regex = '(^%s\.(?:part(?!0*1\.rar$)\d+\.rar$|[rstuvw]\d+$))'
        extr_files = []

        # Check input variables
        if not folder:
            folder = self.conf('from')

        check_file_date = True
        if movie_folder:
            check_file_date = False

        if not files:
            for root, folders, names in os.walk(folder):
                files.extend([os.path.join(root, name) for name in names])

        # Find all archive files
        archives = [re.search(archive_regex, name).groupdict() for name in files if re.search(archive_regex, name)]

        #Extract all found archives
        for archive in archives:
            # Check if it has already been processed by CPS
            if self.hastagDir(os.path.dirname(archive['file'])):
                continue

            # Find all related archive files
            archive['files'] = [name for name in files if re.search(restfile_regex % re.escape(archive['base']), name)]
            archive['files'].append(archive['file'])

            # Check if archive is fresh and maybe still copying/moving/downloading, ignore files newer than 1 minute
            if check_file_date:
                file_too_new = False
                for cur_file in archive['files']:
                    if not os.path.isfile(cur_file):
                        file_too_new = time.time()
                        break
                    file_time = [os.path.getmtime(cur_file), os.path.getctime(cur_file)]
                    for t in file_time:
                        if t > time.time() - 60:
                            file_too_new = tryInt(time.time() - t)
                            break

                    if file_too_new:
                        break

                if file_too_new:
                    try:
                        time_string = time.ctime(file_time[0])
                    except:
                        try:
                            time_string = time.ctime(file_time[1])
                        except:
                            time_string = 'unknown'

                    log.info('Archive seems to be still copying/moving/downloading or just copied/moved/downloaded (created on %s), ignoring for now: %s', (time_string, os.path.basename(archive['file'])))
                    continue

            log.info('Archive %s found. Extracting...', os.path.basename(archive['file']))
            try:
                rar_handle = RarFile(archive['file'])
                extr_path = os.path.join(self.conf('from'), os.path.relpath(os.path.dirname(archive['file']), folder))
                self.makeDir(extr_path)
                for packedinfo in rar_handle.infolist():
                    if not packedinfo.isdir and not os.path.isfile(os.path.join(extr_path, os.path.basename(packedinfo.filename))):
                        log.debug('Extracting %s...', packedinfo.filename)
                        rar_handle.extract(condition = [packedinfo.index], path = extr_path, withSubpath = False, overwrite = False)
                        extr_files.append(os.path.join(extr_path, os.path.basename(packedinfo.filename)))
                del rar_handle
            except Exception, e:
                log.error('Failed to extract %s: %s %s', (archive['file'], e, traceback.format_exc()))
                continue

            # Delete the archive files
            for filename in archive['files']:
                if cleanup:
                    try:
                        os.remove(filename)
                    except Exception, e:
                        log.error('Failed to remove %s: %s %s', (filename, e, traceback.format_exc()))
                        continue
                files.remove(filename)

        # Move the rest of the files and folders if any files are extracted to the from folder (only if folder was provided)
        if extr_files and os.path.normpath(os.path.normcase(folder)) != os.path.normpath(os.path.normcase(self.conf('from'))):
            for leftoverfile in list(files):
                move_to = os.path.join(self.conf('from'), os.path.relpath(leftoverfile, folder))

                try:
                    self.makeDir(os.path.dirname(move_to))
                    self.moveFile(leftoverfile, move_to, cleanup)
                except Exception, e:
                    log.error('Failed moving left over file %s to %s: %s %s', (leftoverfile, move_to, e, traceback.format_exc()))
                    # As we probably tried to overwrite the nfo file, check if it exists and then remove the original
                    if os.path.isfile(move_to):
                        if cleanup:
                            log.info('Deleting left over file %s instead...', leftoverfile)
                            os.unlink(leftoverfile)
                    else:
                        continue

                files.remove(leftoverfile)
                extr_files.append(move_to)

            if cleanup:
                # Remove all left over folders
                log.debug('Removing old movie folder %s...', movie_folder)
                self.deleteEmptyFolder(movie_folder)

            movie_folder = os.path.join(self.conf('from'), os.path.relpath(movie_folder, folder))
            folder = self.conf('from')

        if extr_files:
            files.extend(extr_files)

        # Cleanup files and folder if movie_folder was not provided
        if not movie_folder:
            files = []
            folder = None

        return folder, movie_folder, files, extr_files
