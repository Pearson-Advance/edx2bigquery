#!/usr/bin/python
#
# File:   make_person_course.py
# Date:   13-Oct-14
# Author: I. Chuang <ichuang@mit.edu>
#
# make the person_course dataset for a specified course, using Google BigQuery
# and data stored in Google cloud storage.
#
# each entry has the following fields:
# 
# course_id
# user_id
# username
# registered (bool)
# viewed+ (bool)
# viewed (bool)
# explored+ (bool)
# explored (bool)
# explored_by_logs+ (bool)
# explored_by_logs (bool)
# certified+ (bool)
# certified (bool)
# ip
# cc_by_ip
# LoE (level of education)
# YoB (year of birth)
# gender
# grade (from certificate table)
# start_time (registration time, from enrollment table)
# last_event (time, from derived_person_last_active)
# nevents (total number of events in tracking_log, from derived_daily_event_counts)
# ndays_act (number of days with activity in the tracking_log, up to end-date)
# nplay_video (number of play_video events for this user & course)
# nchapters (number of chapters viewed in course by given student)
# nforum_posts (number of forum posts for user & course)
# roles (empty for students, otherwise "instructor" or "staff" if had that role in the course)
# nprogcheck (number of progress checks)
# nproblem_check (number of problem checks)
# nforum_events (number of forum views)
# mode (kind of enrollment, e.g. honor, idverified)
#
# performs queries needed to compute derivative tables, including:
#
# - pc_nevents
# - pc_modal_ip
#
# Assembles final person_course dataset locally, then uploads back to GS & BQ.
#
# Usage:
#
#    python make_person_course.py course_directory
#
# e.g.:
#
#    python make_person_course.py 6.SFMx

import os
import sys
import csv
import gzip
import json
import bqutil
import gsutil
import datetime
import copy
from path import path
from collections import OrderedDict, defaultdict
from check_schema_tracking_log import schema2dict, check_schema
from load_course_sql import find_course_sql_dir

#-----------------------------------------------------------------------------

class PersonCourse(object):
    
    def __init__(self, course_id, course_dir=None, course_dir_root='', course_dir_date='', 
                 verbose=True, gsbucket="gs://x-data", 
                 force_recompute_from_logs=False,
                 start_date = '2012-01-01',
                 end_date = '2014-09-21',
                 ):

        self.course_id = course_id
        self.course_dir = find_course_sql_dir(course_id, course_dir_root, course_dir_date)
        self.cdir = path(self.course_dir)
        self.logmsg = []

        if not self.cdir.exists():
            print "Oops: missing directory %s!" % self.cdir
            sys.exit(-1)

        self.verbose = verbose
        self.force_recompute_from_logs = force_recompute_from_logs

        self.gspath = gsutil.gs_path_from_course_id(course_id, gsbucket)

        self.dataset = bqutil.course_id2dataset(course_id)
        self.log("dataset=%s" % self.dataset)

        self.end_date = end_date
        self.start_date = start_date
        # self.start_date = '2014-08-01'			# for debugging - smaller BQ queries
        self.sql_parameters = {'dataset': self.dataset, 
                               'end_date': self.end_date, 
                               'start_date': self.start_date,
                               'course_id': self.course_id,
                               }

        mypath = os.path.dirname(os.path.realpath(__file__))
        self.SCHEMA_FILE = '%s/schemas/schema_person_course.json' % mypath

        try:
            self.the_schema = json.loads(open(self.SCHEMA_FILE).read())['person_course']
        except Exception as err:
            print "Oops!  Failed to load schema file for person course.  Error: %s" % str(err)
            raise

        self.the_dict_schema = schema2dict(copy.deepcopy(self.the_schema))
        self.pctab = OrderedDict()

        self.log("="*100)
        self.log("Person Course initialized, course_dir=%s, dataset=%s (started %s)" % (self.cdir, self.dataset, datetime.datetime.now()))
        self.log("="*100)

    def log(self, msg):
        self.logmsg.append(msg)
        if self.verbose:
            print msg
            sys.stdout.flush()

    def openfile(self, fn, mode='r'):
        if fn.endswith('.gz'):
            return gzip.GzipFile(self.cdir / fn, mode)
        return open(self.cdir / fn, mode)
    
    def load_csv(self, fn, key, schema=None, multi=False, fields=None, keymap=None):
        '''
        load csv file into memory, storing into dict with specified field (key) as the key.
        if multi, then each dict value is a list, with one or more values per key.
    
        if fields, load only those specified fields.
        '''
        data = OrderedDict()
        if keymap is None:
            keymap = lambda x: x
        for line in csv.DictReader(openfile(fn)):
            try:
                the_id = keymap(line[key])
            except Exception as err:
                self.log("oops, failed to do keymap, key=%s, line=%s" % (line[key], line))
                raise
            if fields:
                newline = { x: line[x] for x in fields }
                line = newline
            if multi:
                if the_id in data:
                    data[the_id].append(line)
                else:
                    data[the_id] = [ line ]
            else:
                data[the_id] = line
        return data
    
    def load_json(self, fn, key):
        data = OrderedDict()
        cnt = 0
        for line in self.openfile(fn):
            cnt += 1
            jd = json.loads(line)
            if key in jd:
                the_id = jd[key]
                data[the_id] = jd
            else:
                print "[make_person_course] oops!  missing %s from %s row %s=%s" % (key, fn, cnt, jd)
        return data
    
    @staticmethod
    def copy_fields(src, dst, fields):
        for key, val in fields.items():
            if type(val)==list:
                for valent in val:
                    if valent in src:
                        dst[key] = src[valent]
                        break
            else:
                if val in src:
                    dst[key] = src[val]
    
    #-----------------------------------------------------------------------------
    
    def get_nchapters_from_course_structure(self):
        nchapters = 0

        def getonefile(files):
            for fn in files:
                if (self.cdir / fn).exists():
                    return fn
            raise Exception('Cannot find required file in %s, one of %s' % (self.cdir, files))

        csfn = getonefile(['course_structure-prod-analytics.json.gz', 'course_structure.json'])
        struct = json.loads(self.openfile(csfn).read())
        for key, ent in struct.iteritems():
            if ent['category']=='chapter':
                nchapters += 1

        self.log('course %s has %s chapters, based on %s' % (self.course_id, nchapters, csfn))
        self.nchapters = nchapters

        return nchapters
    
    def reload_table(self):
        '''
        load current verison of the person course table from an existing
        person_course.json.gz file
        
        This is useful to do, for example, when the second or third phases of 
        the person course dataset need to be added to an existing dataset.
        '''
        dfn = 'person_course.json.gz'
        data = {}
        for line in self.openfile(dfn):
            dline = json.loads(line)
            key = dline['username']

            if 'city' in dline:
                dline['city'] = dline['city'].encode('utf8')
            if 'countryLabel' in dline:
                dline['countryLabel'] = dline['countryLabel'].encode('utf8')

            data[key] = dline
        self.pctab = data
        self.log("Loaded %d lines from %s" % (len(data), dfn))

    def compute_first_phase(self):
    
        # -----------------------------------------------------------------------------
        # person_course part 1: stuff which can be computed using just the user_info_combo table

        self.log("-"*20)
        self.log("Computing first phase based on user_info_combo")
        
        uicdat = self.load_json('user_info_combo.json.gz', 'username') # start with user_info_combo file, use username as key becase of tracking logs
        self.uicdat = uicdat
    
        for key, uicent in uicdat.iteritems():
            pcent = OrderedDict()
            self.pctab[key] = pcent			# key = username
            self.copy_fields(uicent, pcent,
                             {'course_id': ['enrollment_course_id', 'certificate_course_id'],
                              'user_id': 'user_id',
                              'username': 'username',
                              'gender': 'profile_gender',
                              'LoE': 'profile_level_of_education',
                              'YoB': 'profile_year_of_birth',
                              'grade': 'certificate_grade',
                              'start_time': 'enrollment_created',
                              'mode': 'enrollment_mode',
                              })
        
            pcent['registered'] = True	# by definition
        
            # derived entries, from SQL data
        
            # certificate status can be [ "downloadable", "notpassing", "unavailable" ]
            if uicent.get('certificate_status', '') in [ "downloadable","unavailable" ]:
                pcent['certified'] = True
            else:
                pcent['certified'] = False
        
    def compute_second_phase(self):
    
        # -----------------------------------------------------------------------------
        # person_course part 2: stuff which can be computed using SQL tables, and no tracking log queries
        
        self.log("-"*20)
        self.log("Computing second phase based on SQL table queries done in BigQuery")

        self.load_nchapters()
        self.load_pc_forum()
        nchapters = self.get_nchapters_from_course_structure()

        for key, pcent in self.pctab.iteritems():

            uid = str(pcent['user_id'])
            if uid in self.pc_nchapters['data_by_key']:
                pcent['viewed'] = True
                pcent['nchapters'] = int(self.pc_nchapters['data_by_key'][uid]['nchapters'])
                if int(self.pc_nchapters['data_by_key'][uid]['nchapters']) >= nchapters/2:
                    pcent['explored'] = True
                else:
                    pcent['explored'] = False
            else:
                pcent['viewed'] = False

            for field in [  ['nforum', 'nforum_posts'],
                            ['nvotes', 'nforum_votes'],
                            ['nendorsed', 'nforum_endorsed'],
                            ['nthread', 'nforum_threads'],
                            ['ncomment', 'nforum_comments'],
                            ['npinned', 'nforum_pinned'],
                          ]:
                self.copy_from_bq_table(self.pc_forum, pcent, uid, field)

    @staticmethod
    def copy_from_bq_table(src, dst, username, field, new_field=None, mkutf=False):
        '''
        Copy fields from downloaded BQ table dict (src) into the person course dict (dst).
        '''
        if type(field) is list:
            (field, new_field) = field
        datum = src['data_by_key'].get(username, {}).get(field, None)
        if datum is not None:
            if new_field is not None:
                dst[new_field] = datum
            else:
                dst[field] = datum
            if mkutf:
                dst[field] = dst[field].encode('utf8')

    def compute_third_phase(self):
    
        # -----------------------------------------------------------------------------
        # person_course part 3: stuff which needs tracking log queries
        
        self.log("-"*20)
        self.log("Computing third phase based on tracking log table queries done in BigQuery")

        self.load_last_event()
        self.load_pc_day_totals()	# person-course-day totals contains all the aggregate nevents, etc.
        self.load_modal_ip()

        pcd_fields = ['nevents', 'ndays_act', 'nprogcheck', 'nshow_answer', 'nvideo', 'nproblem_check', 
                      ['nforum', 'nforum_events'], 'ntranscript', 'nseq_goto',
                      ['nvideo', 'nplay_video'],
                      'nseek_video', 'npause_video', 'avg_dt', 'sdv_dt', 'max_dt', 'n_dt', 'sum_dt']

        for key, pcent in self.pctab.iteritems():
            username = pcent['username']

            # pcent['nevents'] = self.pc_nevents['data_by_key'].get(username, {}).get('nevents', None)

            le = self.pc_last_event['data_by_key'].get(username, {}).get('last_event', None)
            if le is not None and le:
                try:
                    le = str(datetime.datetime.utcfromtimestamp(float(le)))
                except Exception as err:
                    self.log('oops, last event cannot be turned into a time; le=%s, username=%s' % (le, username))
                pcent['last_event'] = le

            self.copy_from_bq_table(self.pc_modal_ip, pcent, username, 'modal_ip', new_field='ip')

            for pcdf in pcd_fields:
                self.copy_from_bq_table(self.pc_day_totals, pcent, username, pcdf)

    def compute_fourth_phase(self):
    
        # -----------------------------------------------------------------------------
        # person_course part 4: geoip (needs modal ip and maxmind geoip dataset)
        
        self.log("-"*20)
        self.log("Computing fourth phase based on modal_ip and geoip join in BigQuery")

        self.load_pc_geoip()

        pcd_fields = [['country', 'cc_by_ip'], 'latitude', 'longitude']

        for key, pcent in self.pctab.iteritems():
            username = pcent['username']

            for pcdf in pcd_fields:
                self.copy_from_bq_table(self.pc_geoip, pcent, username, pcdf)
            self.copy_from_bq_table(self.pc_geoip, pcent, username, 'countryLabel', mkutf=True)	# unicode
            self.copy_from_bq_table(self.pc_geoip, pcent, username, 'city', mkutf=True)		# unicode

    def output_table(self):
        '''
        output person_course table 
        '''
        
        fieldnames = self.the_dict_schema.keys()
        ofn = 'person_course.csv.gz'
        ofnj = 'person_course.json.gz'
        ofp = self.openfile(ofnj, 'w')
        ocsv = csv.DictWriter(self.openfile(ofn, 'w'), fieldnames=fieldnames)
        ocsv.writeheader()
        
        self.log("Writing output to %s and %s" % (ofn, ofnj))

        # write JSON first - it's safer
        cnt = 0
        for key, pcent in self.pctab.iteritems():
            cnt += 1
            check_schema(cnt, pcent, the_ds=self.the_dict_schema, coerce=True)
            ofp.write(json.dumps(pcent) + '\n')

        # now write CSV file (may have errors due to unicode)
        for key, pcent in self.pctab.iteritems():
            try:
                ocsv.writerow(pcent)
            except Exception as err:
                self.log("Error writing CSV output row=%s" % pcent)
                raise
        
    def upload_to_bigquery(self):
        '''
        upload person_course table to bigquery, via google cloud storage
        '''
        
        def upload_to_gs(fn):
            ofn = self.cdir / fn
            gsfn = self.gspath + '/'
            gsfnp = gsfn + fn			# full path to google storage data file
            cmd = 'gsutil cp %s %s' % (ofn, gsfn)
            self.log("Uploading to gse using %s" % cmd)
            os.system(cmd)
            return gsfnp

        gsfnp = upload_to_gs('person_course.json.gz')
        upload_to_gs('person_course.csv.gz')

        tableid = "person_course"
        bqutil.load_data_to_table(self.dataset, tableid, gsfnp, self.the_schema, wait=True, verbose=False)

        description = '\n'.join(self.logmsg)
        description += "Person course for %s with nchapters=%s, start=%s, end=%s\n" % (self.course_id,
                                                                                       self.nchapters,
                                                                                       self.start_date,
                                                                                       self.end_date,
                                                                                       )
        description += "course SQL directory = %s\n" % self.course_dir
        description += "="*100
        description += "\nDone at %s" % datetime.datetime.now()
        bqutil.add_description_to_table(self.dataset, tableid, description, append=True)

    def load_nchapters(self):
        
        the_sql = '''
        select user_id, count(*) as nchapters from (
            SELECT student_id as user_id, module_id, count(*) as chapter_views
            FROM [{dataset}.studentmodule]
            # FROM [{dataset}.courseware_studentmodule]
            where module_type = "chapter"
            group by user_id, module_id
        )
        group by user_id
        order by user_id
        '''.format(**self.sql_parameters)
        
        tablename = 'pc_nchapters'

        self.log("Loading %s from BigQuery" % tablename)
        self.pc_nchapters = bqutil.get_bq_table(self.dataset, tablename, the_sql, key={'name': 'user_id'}, logger=self.log)

    def load_pc_day_totals(self):
        '''
        Compute a single table aggregating all the person_course_day table data, into a single place.
        '''
        
        the_sql = '''
            select username, 
                "{course_id}" as course_id,
                count(*) as ndays_act,
                sum(nevents) as nevents,
                sum(nprogcheck) as nprogcheck,
                sum(nshow_answer) as nshow_answer,
                sum(nvideo) as nvideo,
                sum(nproblem_check) as nproblem_check,
                sum(nforum) as nforum,
                sum(ntranscript) as ntranscript,
                sum(nseq_goto) as nseq_goto,
                sum(nseek_video) as nseek_video,
                sum(npause_video) as npause_video,
                AVG(avg_dt) as avg_dt,
                sqrt(sum(sdv_dt*sdv_dt * n_dt)/sum(n_dt)) as sdv_dt,
                MAX(max_dt) as max_dt,
                sum(n_dt) as n_dt,
                sum(sum_dt) as sum_dt
            from
                (TABLE_DATE_RANGE( 
                      [{dataset}_pcday.pcday_],                                                                                                               
                      TIMESTAMP('{start_date}'), TIMESTAMP('{end_date}')))
            group by username
            order by sum_dt desc
        '''.format(**self.sql_parameters)
        
        tablename = 'pc_day_totals'

        self.log("Loading %s from BigQuery" % tablename)
        setattr(self, tablename, bqutil.get_bq_table(self.dataset, tablename, the_sql, key={'name': 'username'},
                                                     force_query=self.force_recompute_from_logs, logger=self.log))

    def load_pc_forum(self):
        '''
        Compute statistics about forum use by user.
        '''
        
        the_sql = '''
            SELECT author_id as user_id, 
                   count(*) as nforum,
                   sum(votes.count) as nvotes,
                   sum(case when pinned then 1 else 0 end) as npinned,
                   sum(case when endorsed then 1 else 0 end) as nendorsed,
                   sum(case when _type="CommentThread" then 1 else 0 end) as nthread,
                   sum(case when _type="Comment" then 1 else 0 end) as ncomment,
            FROM [{dataset}.forum] 
            group by user_id
            order by nthread desc
        '''.format(**self.sql_parameters)
        
        tablename = 'pc_forum'

        self.log("Loading %s from BigQuery" % tablename)
        setattr(self, tablename, bqutil.get_bq_table(self.dataset, tablename, the_sql, key={'name': 'user_id'},
                                                     force_query=self.force_recompute_from_logs, logger=self.log))
        
    def load_last_event(self):
        
        the_sql = '''
        SELECT username, max(time) as last_event
            FROM (TABLE_DATE_RANGE(
                                   [{dataset}_logs.tracklog_], 
                                   TIMESTAMP('{start_date}'), TIMESTAMP('{end_date}')))
            where username != "" 
            group by username
        order by username
        '''.format(**self.sql_parameters)
        
        tablename = 'pc_last_event'

        self.log("Loading %s from BigQuery" % tablename)
        setattr(self, tablename, bqutil.get_bq_table(self.dataset, tablename, the_sql, key={'name': 'username'},
                                                     force_query=self.force_recompute_from_logs, logger=self.log))

    def ensure_all_daily_tracking_logs_loaded(self):
        '''
        Check to make sure all the needed *_logs.tracklog_* tables exist.
        '''
        return self.ensure_all_daily_tables_loaded('logs', 'tracklog')

    def ensure_all_pc_day_tables_loaded(self):
        '''
        Check to make sure all the needed *_pcday.pcday_* tables exist.
        '''
        return self.ensure_all_daily_tables_loaded('pcday', 'pcday')

    def ensure_all_daily_tables_loaded(self, dsuffix, tprefix):
        dataset = self.dataset + "_" + dsuffix
        tables_info = bqutil.get_tables(dataset)['tables']
        tables = [ x['tableReference']['tableId'] for x in tables_info ]
        
        def daterange(start, end):
            k = start
            dates = []
            while (k <= end):
                dates.append('%04d%02d%02d' % (k.year, k.month, k.day))
                k += datetime.timedelta(days=1)
            return dates

        def d2dt(date):
            return datetime.datetime.strptime(date, '%Y-%m-%d')

        for k in daterange(d2dt(self.start_date), d2dt(self.end_date)):
            tname = tprefix + '_' + str(k)
            if tname not in tables:
                msg = "Oops, missing needed table %s from database %s" % (tname, dataset)
                self.log(msg)
                self.tables = tables
                raise Exception(msg)
        return

    def load_nevents(self):
        '''
        '''
        
        the_sql = '''
        SELECT username, count(*) as nevents
            FROM (TABLE_DATE_RANGE(
                                   [{dataset}_logs.tracklog_], 
                                   TIMESTAMP('{start_date}'), TIMESTAMP('{end_date}')))
            where username != "" 
            group by username
        order by username
        '''.format(**self.sql_parameters)
        
        tablename = 'pc_nevents'

        self.log("Loading %s from BigQuery" % tablename)
        setattr(self, tablename, bqutil.get_bq_table(self.dataset, tablename, the_sql, key={'name': 'username'},
                                                     force_query=self.force_recompute_from_logs, logger=self.log))

    def load_modal_ip(self):
        '''
        '''
        
        the_sql = '''
        SELECT username, IP as modal_ip, ip_count from
        ( SELECT username, IP, ip_count, 
            RANK() over (partition by username order by ip_count DESC) rank,
          from
          ( SELECT username, IP, count(IP) as ip_count
            FROM (TABLE_DATE_RANGE(
                                   [{dataset}_logs.tracklog_], 
                                   TIMESTAMP('{start_date}'), TIMESTAMP('{end_date}')))
            where username != "" 
            group by username, IP
          )
        )
        where rank=1
        order by username, rank
        '''.format(**self.sql_parameters)
        
        tablename = 'pc_modal_ip'

        self.log("Loading %s from BigQuery" % tablename)
        setattr(self, tablename, bqutil.get_bq_table(self.dataset, tablename, the_sql, key={'name': 'username'},
                                                     force_query=self.force_recompute_from_logs, logger=self.log))

    def load_pc_geoip(self):
        '''
        geoip information from modal_ip, using bigquery join with maxmine public geoip dataset
        http://googlecloudplatform.blogspot.com/2014/03/geoip-geolocation-with-google-bigquery.html        
        '''
        the_sql = '''
            SELECT username, country, city, countryLabel, latitude, longitude
            FROM (
             SELECT
               username,
               INTEGER(PARSE_IP(modal_ip)) AS clientIpNum,
               INTEGER(PARSE_IP(modal_ip)/(256*256)) AS classB
             FROM
               [{dataset}.pc_modal_ip]
             WHERE modal_ip IS NOT NULL
               ) AS a
            JOIN EACH [fh-bigquery:geocode.geolite_city_bq_b2b] AS b
            ON a.classB = b.classB
            WHERE a.clientIpNum BETWEEN b.startIpNum AND b.endIpNum
            AND city != ''
            ORDER BY username
        '''.format(**self.sql_parameters)
        
        tablename = 'pc_geoip'

        self.log("Loading %s from BigQuery" % tablename)
        setattr(self, tablename, bqutil.get_bq_table(self.dataset, tablename, the_sql, key={'name': 'username'},
                                                     force_query=self.force_recompute_from_logs, logger=self.log))

    def load_cwsm(self):
        self.cwsm = load_csv('studentmodule.csv', 'student_id', fields=['module_id', 'module_type'], keymap=int)

    def make_all(self):
        self.compute_first_phase()
        self.compute_second_phase()
        self.compute_third_phase()
        self.compute_fourth_phase()
        self.output_table()
        self.upload_to_bigquery()

    def redo_second_phase(self):
        self.log("PersonCourse just re-doing second phase")
        self.reload_table()
        self.compute_second_phase()
        self.output_table()
        self.upload_to_bigquery()
        
#-----------------------------------------------------------------------------

def make_person_course(course_id, basedir="X-Year-2-data-sql", datedir="2013-09-21", options='', 
                       gsbucket="gs://x-data",
                       start="2012-09-05",
                       end="2013-09-21",
                       force_recompute=False,
                       ):
    '''
    make one person course dataset
    '''
    print "-"*77
    print "Processing person course for %s (start %s)" % (course_id, datetime.datetime.now())
    force_recompute = force_recompute or ('force_recompute' in options)
    if force_recompute:
        print "--> Note: Forcing re-querying of person_day results from tracking logs!!! Can be $$$ expensive!!!"
        sys.stdout.flush()
    pc = PersonCourse(course_id, course_dir_root=basedir, course_dir_date=datedir,
                      gsbucket=gsbucket,
                      start_date=start, 
                      end_date=end,
                      force_recompute_from_logs=force_recompute)
    redo2 = 'redo2' in options
    if redo2:
        pc.redo_second_phase()
    else:
        pc.make_all()
    print "Done processing person course for %s (end %s)" % (course_id, datetime.datetime.now())
    print "-"*77
        
#-----------------------------------------------------------------------------

def make_pc(*args):
    pc = PersonCourse(**args)
    pc.make_all()
    return pc

def make_pc3(course_id, cdir):
    pc = PersonCourse(course_id, cdir)
    pc.reload_table()
    pc.compute_third_phase()
    pc.output_table()
    pc.upload_to_bigquery()
    return pc
