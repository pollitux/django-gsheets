from googleapiclient.discovery import build
from .auth import get_gapi_credentials
import re
import logging

logger = logging.getLogger(__name__)


class BaseGoogleSheetMixin(object):
    """ base mixin for google sheets """
    # ID of a Google Sheets spreadsheet
    spreadsheet_id = None
    # name of the sheet inside the spreadsheet to use
    sheet_name = 'Sheet1'
    # range of data in the sheet
    data_range = 'A1:Z'
    # name of the field to use as the ID field for model instances in the sync'd sheet
    model_id_field = 'id'
    # name of the sheet column to use to store the ID of the Django model instance
    sheet_id_field = 'Django GUID'
    # the batch size determines at what point sheet data is written-out to the Google sheet
    batch_size = 500
    # the max rows to support in the sheet
    max_rows = 30000
    # max column to support in the sheet
    max_col = 'Z'

    def __init__(self, *args, **kwargs):
        super(BaseGoogleSheetMixin, self).__init__(*args, **kwargs)
        self._api = None
        self._credentials = None
        self._sheet_data = None
        self._sheet_headers = None

    @property
    def credentials(self):
        """ gets an Credentials instance to use for request auth
        :return `google.oauth2.Credentials`
        :raises: `ValueError` if no credentials have been created
        """
        from .models import AccessCredentials

        if self._credentials:
            return self._credentials

        ac = AccessCredentials.objects.order_by('-created_time').first()
        if ac is None:
            raise ValueError('you must authenticate gsheets at /gsheets/authorize/ before usage')

        self._credentials = get_gapi_credentials(ac)

        return self._credentials

    @property
    def api(self):
        if self._api is not None:
            return self._api

        self._api = build('sheets', 'v4', credentials=self.credentials)
        return self._api

    @property
    def sheet_data(self):
        if self._sheet_data is not None:
            return self._sheet_data

        api_res = self.api.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range=self.sheet_range).execute()
        self._sheet_data = api_res.get('values', [])
        self._sheet_headers = self._sheet_data[0]
        # remove the headers from the data
        self._sheet_data = self._sheet_data[1:]

        return self._sheet_data

    @property
    def sheet_headers(self):
        if not self._sheet_headers:
            # self.sheet_data sets the headers
            noop = self.sheet_data

        return self._sheet_headers

    @property
    def sheet_range(self):
        return BaseGoogleSheetMixin.get_sheet_range(self.sheet_name, self.data_range)

    @property
    def sheet_range_rows(self):
        """
        :return: `two-tuple`
        """
        row_match = re.search('[A-Z]+(\d+):[A-Z]+(\d*)', self.sheet_range)
        try:
            start, end = row_match.groups()
        except ValueError:
            start, end = row_match.groups()[0], self.max_rows

        if end == '':
            end = self.max_rows

        return int(start), int(end)

    @property
    def sheet_range_cols(self):
        """
        :return: `two-tuple`
        """
        col_match = re.search('([A-Z]+)\d*:([A-Z]+)\d*', self.sheet_range)
        try:
            start, end = col_match.groups()
        except ValueError:
            start, end = col_match.groups()[0], self.max_col

        return start, end

    @staticmethod
    def get_sheet_range(sheet_name, data_range):
        return '!'.join([sheet_name, data_range])

    def column_index(self, field_name):
        """ given a canonical field name (like 'Name'), get the column index of that field in the sheet. This relies
        on the first row in the sheet having a cell with the name of the given field
        :param field_name: `str`
        :return: `int` index of the column in the sheet storing the given fields' data
        :raises: `ValueError` if the field name doesn't exist in the header row
        """
        logger.debug(f'got header row {self.sheet_headers}')

        return self.sheet_headers.index(field_name)

    def existing_row(self, **data):
        """ given the data to be synced to a row, check if it already exists in the sheet and - if it does - return
        its index
        :param data: `dict` of fields/values
        :return: `int` the index of the row containing the ID if it exists, None otherwise
        :raises: `KeyError` if the data doesn't contain the ID field for the model
        :raises: `ValueError` if the columns don't contain the Sheet ID col
        """
        model_id = data[self.model_id_field]
        sheet_id_ix = self.column_index(self.sheet_id_field)

        # look through the sheet ID column for the model ID
        for i, r in enumerate(self.sheet_data):
            if r[sheet_id_ix] == model_id:
                return i

        return None


class SheetPushableMixin(BaseGoogleSheetMixin):
    """ mixes in functionality to push data to a google sheet """
    def upsert_table(self):
        """ upserts objects of this instance type to Sheets """
        queryset = self.__class__.get_queryset()
        last_writeout = 0
        cols_start, cols_end = self.sheet_range_cols
        rows_start, rows_end = self.sheet_range_rows

        for i, obj in enumerate(queryset):
            if i > 0 and i % self.batch_size == 0:
                writeout_range_start_row = (rows_start + 1) + i
                writeout_range_end_row = writeout_range_start_row + self.batch_size
                writeout_range = BaseGoogleSheetMixin.get_sheet_range(
                    self.sheet_name, f'{cols_start}{writeout_range_start_row}:{cols_end}{writeout_range_end_row}'
                )

                writeout_data_start_row = (rows_start - 1) + i
                writeout_data_end_row = writeout_data_start_row + self.batch_size
                writeout_data = self.sheet_data[writeout_data_start_row:writeout_data_end_row]

                logger.debug(f'writing out {len(writeout_data)} rows of data to {writeout_range}')

                self.writeout(writeout_range, writeout_data)
                last_writeout = i

            push_data = {f: getattr(obj, f) for f in self.__class__.get_push_fields()}
            self.upsert_data(**push_data)

        # writeout any remaining data
        if last_writeout < len(queryset):
            logger.debug(f'writing out {len(queryset) - last_writeout} final rows of data')
            writeout_range = BaseGoogleSheetMixin.get_sheet_range(
                self.sheet_name, f'{cols_start}{max(2, last_writeout)}:{cols_end}{rows_end}'
            )
            self.writeout(writeout_range, self.sheet_data[last_writeout:])

        logger.info('FINISHED WITH TABLE UPSERT')

    @classmethod
    def get_queryset(cls):
        return cls.objects.all()

    @classmethod
    def get_push_fields(cls):
        """ get the field names from the model which are to be pushed. MUST INCLUDE THE model_id_field """
        return [f.name for f in cls._meta.fields]

    def upsert_data(self, **data):
        """ upserts the data, given as a dict of field/values, to the sheet. If the data already exists, replaces
        its previous value
        :param data: `dict` of field/value
        """
        field_indexes = []
        for field in data.keys():
            try:
                field_indexes.append((field, self.column_index(field if field != self.model_id_field else self.sheet_id_field)))
            except ValueError:
                logger.info(f'skipping field {field} because it has no header')

        # order the field indexes by their col index
        sorted_field_indexes = sorted(field_indexes, key=lambda x: x[1])

        row_data = []
        for field, ix in sorted_field_indexes:
            logger.debug(f'writing data in field {field} to col ix {ix}')
            row_data.append(data[field])

        # get the row to update if it exists, otherwise we will add a new row
        existing_row_ix = self.existing_row(**data)
        if existing_row_ix is not None:
            self.sheet_data[existing_row_ix] = row_data
        else:
            self.sheet_data.append(row_data)

    def writeout(self, range, data):
        """ writes the given data to the given range in the spreadsheet
        :param range: `str` a range (like 'Sheet1!A2:B3') to write data to
        :param data: `list` of `list` the set of data to write
        """
        request_body = {
            'value_input_option': 'RAW',
            'data': {
                'range': range,
                'values': data
            }
        }

        request = self.api.spreadsheets().values().batchUpdate(spreadsheetId=self.spreadsheet_id, body=request_body)
        response = request.execute()

        logger.debug(f'got response {response} executing writeout in range {range}')

        return response


class SheetPullableMixin(object):
    """ mixes in functionality to pull data from a google sheet """
    pass


class SheetSyncableMixin(SheetPushableMixin, SheetPullableMixin):
    """ mixes in ability to 2-way sync data from/to a google sheet """
    pass