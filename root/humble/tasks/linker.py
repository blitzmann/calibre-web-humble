from __future__ import division, print_function, unicode_literals
import os
from shutil import copyfile
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from cps import logger, config, db, helper
from cps.db import Data
from cps.services.worker import CalibreTask
from cps.uploader import process
from cps.constants import BookMeta
from flask_babel import get_locale

from .constants import TASK_RETENTION_MIN
from ...editbooks import move_coverfile

log = logger.create()

from ... import calibre_db

from contextlib import contextmanager
@contextmanager
def test_database():
    global calibre_db
    old_session = calibre_db
    calibre_db = db.CalibreDB()
    yield
    calibre_db = old_session

class TaskDownloadLinker(CalibreTask):
    def __init__(self, task_message, bundle_name, product_name, tasks):
        super(TaskDownloadLinker, self).__init__(task_message)
        self.bundle_name = bundle_name
        self.product_name = product_name
        self.tasks = tasks

        self.results = dict()

    def run(self, worker_thread):
        # The download tasks associated with this processing task
        task_ids = self.tasks
        bundle_name = self.bundle_name
        product_name = self.product_name
        locale = get_locale()
        # find all the objects with these tasks
        dl_tasks = [x.task for x in worker_thread.tasks if x.task.id in task_ids and x.task.success]
        if len(dl_tasks) == 0:
            return self._handleError("No download tasks finished successfully")

        original_filename = dl_tasks[0].results["filename_root"]

        try:
            processed = []
            # we loop through the DL items start collecting their metadata
            for task in dl_tasks:
                processed.append(process(
                    task.results["filepath"],
                    task.results["filename_root"],
                    task.results["file_extension"],
                    ''  # todo: figure this guy out
                ))

            # cbz metadata is horrible at collecting the cover image. Use cbz as last resort
            cover_priority = lambda x: x.extension == '.cbz'

            # create a new BaseMeta tuple that holds the final values that we will use. We aggregate the various downloaded
            # metadata here
            meta = BookMeta(
                file_path=processed[0].file_path,
                extension=processed[0].extension,
                # We know the title from the bundle itself. Hardcode it here to avoid issue where we can't get any metadata, and thus don't know what book this is
                title=next((meta.title for meta in processed if meta.title != original_filename), None) or product_name,
                author=next((meta.author for meta in processed if meta.author != u'Unknown'), None) or u'Unknown',
                cover=next((meta.cover for meta in sorted(processed, key=cover_priority) if meta.cover is not None),
                           None),
                description=next((meta.description for meta in processed if meta.description not in (None, "")),
                                 None) or "",
                tags=(next((meta.tags for meta in processed if meta.tags not in (None, "")),
                           None) or "") + ", Humble Bundle: " + bundle_name,
                series=next((meta.series for meta in processed if meta.series not in (None, "")), None) or "",
                series_id=next((meta.series_id for meta in processed if meta.series_id not in (None, "")), None) or "",
                languages=next((meta.languages for meta in processed if meta.languages not in (None, "")), None) or "",
                publisher=next((meta.publisher for meta in processed if meta.publisher not in (None, "")), None) or "",
                pubdate=next((meta.pubdate for meta in processed if meta.pubdate not in (None, "")), None) or "",
                identifiers=[]
            )

            local_session = db.CalibreDB(expire_on_commit=False, init=True)
            local_session.session.connection().connection.connection.create_function('uuid4', 0, lambda: str(uuid4()))
            # todo: check to see if the book exists. If it does, add everything under that format.

            from cps.editbooks import create_book_on_upload  # inline import to avoid circular import
            with local_session.session.no_autoflush:
                db_book, input_authors, title_dir, renamed_authors = create_book_on_upload(False, meta, local_session.session)
                book_id = db_book.id
                title = db_book.title
                error = helper.update_dir_structure(book_id,
                                                    config.config_calibre_dir,
                                                    input_authors[0],
                                                    meta.file_path,
                                                    title_dir + meta.extension.lower(),
                                                    renamed_author=renamed_authors,
                                                    db=local_session)

                move_coverfile(meta, db_book)

                # save data to database, reread data
                local_session.session.commit()

            # From here we should have the book added to the database, now we need to link the other downloads to the book
            for format in processed[1:]:  # slice 1:0 since the 0 indexed was already added
                file_name = db_book.path.rsplit('/', 1)[-1]
                filepath = os.path.normpath(os.path.join(config.config_calibre_dir, db_book.path))
                saved_filename = os.path.join(filepath, file_name + format.extension)

                # check if file path exists, otherwise create it, copy file to calibre path and delete temp file
                if not os.path.exists(filepath):
                    os.makedirs(filepath)

                # move the temp file to the new location
                copyfile(format.file_path, saved_filename)

                ext_upper = format.extension[1:].upper()
                file_size = os.path.getsize(format.file_path)
                is_format = local_session.get_book_format(db_book.id, ext_upper)

                # Format entry already exists, no need to update the database
                if is_format:
                    log.warning('Book format %s already existing', ext_upper)
                else:
                    db_format = Data(db_book.id, ext_upper, file_size, file_name)
                    local_session.session.add(db_format)
                    local_session.session.commit()
                    local_session.update_title_sort(config)

            # link to the processed book
            # self.UIqueue[index]['taskMess'] = "<a href=\"/book/{book_id}\">{message}</a>".format(
            #     book_id=db_book.id,
            #     message=self.UIqueue[index]['taskMess']
            # )

        except SQLAlchemyError as e:
            local_session.session.rollback()
            log.error('Database error: %s', e)
            self._handleError(str(e))
        except Exception as e:
            # gracefully handle eny other error that is thrown
            log.error('Unknown error: %s', e)
            self._handleError(str(e))

        # if everything is successfull, set the download tasks to Success
        # for ui in self.UIqueue:
        #     if ui.get("id", None) in task_ids:
        #         ui['stat'] = STAT_FINISH_SUCCESS

        self._handleSuccess()

    @property
    def dead(self):
        orig_val = super(TaskDownloadLinker, self).dead

        then = self.end_time
        now = datetime.now()
        return orig_val and now > then + timedelta(minutes=TASK_RETENTION_MIN)

    @property
    def name(self):
        return "Humble: Process"

    def __repr__(self):
        return self.message

    @property
    def is_cancellable(self):
        return False
