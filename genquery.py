import itertools
import re
from collections import OrderedDict
from enum import Enum
from irods_errors import END_OF_RESULTSET

def AUTO_CLOSE_QUERIES(): return True

__all__ = [
    "Query",
    "row_iterator",
    "paged_iterator",
    "AS_DICT",
    "AS_LIST",
    "AS_TUPLE",
]

MAX_SQL_ROWS = 256

# An optimization for the GenQuery2 implementation.
_END_OF_RESULTSET_ERROR_STRING_PART = f':{END_OF_RESULTSET}]'

class Option(object):
    """iRODS QueryInp option flags - used internally.

    AUTO_CLOSE, RETURN_TOTAL_ROW_COUNT, and UPPER_CASE_WHERE should not be set
    by calling code, as Query already provides convenient functionality for this.

    See irods: lib/core/include/rodsGenQuery.h
    """
    RETURN_TOTAL_ROW_COUNT = 0x020
    NO_DISTINCT            = 0x040
    QUOTA_QUERY            = 0x080
    AUTO_CLOSE             = 0x100
    UPPER_CASE_WHERE       = 0x200

class row_return_type (object):
    def __init__(self): raise NotImplementedError

class AS_DICT  (row_return_type): pass
class AS_LIST  (row_return_type): pass
class AS_TUPLE (row_return_type): pass

class Parser(Enum):
    """Available GenQuery parsers."""
    GENQUERY1 = 1
    GENQUERY2 = 2 # Experimental.

class GenQuery_Options_Spec_Error(RuntimeError): pass
class GenQuery_Columns_Type_Error(RuntimeError): pass
class GenQuery_Row_Return_Type_Error(RuntimeError): pass


class Query(object):
    """Generator-style genquery iterator.

    :param callback:       iRODS callback
    :param columns:        a list of SELECT column names, or columns as a comma-separated string.
    :param conditions:     (optional) where clause, as a string
    :param output:         (optional) [default=AS_TUPLE] either AS_DICT/AS_LIST/AS_TUPLE
    :param offset:         (optional) starting row (0-based), can be used for pagination
    :param limit:          (optional) maximum amount of results, can be used for pagination
    :param case_sensitive: (optional) set this to False to make the entire where-clause case insensitive
    :param options:        (optional) other OR-ed options to pass to the query (see the Option type above)
    :param parser:         (optional) the GenQuery engine to use. Defaults to Parser.GENQUERY1
    :param order_by:       (optional) order-by clause, as a string. Defaults to an empty string. Only recognized by the GenQuery2 parser

    GenQuery2 parser:

      This is an experimental parser and may change in the future.

      When used, the following applies:
        - The "output", "case_sensitive", and "options" constructor parameters are ignored
        - The "total_rows()" member function always returns None

      Some features of GenQuery2 cannot be expressed via this interface (e.g. GROUP BY). If
      those features are needed, use the GenQuery2 microservices directly.

    Getting the total row count:

      Use q.total_rows() to get the total number of results matching the query
      (without taking offset/limit into account).

    Output types:

      AS_LIST and AS_DICT behave the same as in row_iterator.
      AS_TUPLE produces a tuple, similar to AS_LIST, with the exception that
      for queries on single columns, each result is returned as a string
      instead of a 1-element tuple.

    Examples:

        # Print all collections.
        for x in Query(callback, 'COLL_NAME'):
            print('name: ' + x)

        # The same, but more verbose:
        for x in Query(callback, 'COLL_NAME', output=AS_DICT):
            print('name: {}'.format(x['COLL_NAME']))

        # ... or make it into a list
        colls = list(Query(callback, 'COLL_NAME'))

        # ... or get data object paths
        datas = ['{}/{}'.format(x, y) for x, y in Query(callback, 'COLL_NAME, DATA_NAME')]

        # Print the first 200-299 of data objects ordered descending by data
        # name, owned by a username containing 'r' or 'R', in a collection
        # under (case-insensitive) '/TEMPzone/'.
        for x in Query(callback, 'COLL_NAME, ORDER_DESC(DATA_NAME), DATA_OWNER_NAME',
                       "DATA_OWNER_NAME like '%r%' and COLL_NAME like '/TEMPzone/%'",
                       case_sensitive=False,
                       offset=200, limit=100):
            print('name: {}/{} - owned by {}'.format(*x))
    """

    __parameter_names = tuple('columns,conditions,output,offset,limit,case_sensitive,options,parser,order_by'.split(','))
    __non_whitespace = re.compile('\S+')

    def __init__(self,
                 callback,
                 columns,
                 conditions='',
                 output=AS_TUPLE,
                 offset=0,
                 limit=None,
                 case_sensitive=True,
                 options=0,
                 parser=Parser.GENQUERY1,
                 order_by=''):

        self.callback = callback

        if isinstance(columns, str):
            # Convert to list for caller convenience.
            columns = [x.strip() for x in columns.split(',') if self.__non_whitespace.search(x)]
        else:
            try:    columns = list(columns)
            except: raise GenQuery_Columns_Type_Error("'columns' should be a comma-separated string or sequence")

        if not isinstance (columns, list):
            raise GenQuery_Columns_Type_Error("'columns' could not be coerced to list type")

        if parser not in [Parser.GENQUERY1, Parser.GENQUERY2]:
            raise ValueError('Invalid value for [parser]. Expected Parser.GENQUERY1 or Parser.GENQUERY2.')

        # Options as specified
        self.columns        = columns     # - via 2nd argument to ctor; or copy() 'columns' keyword option
        self.conditions     = conditions
        self.output         = output
        self.offset         = offset
        self.limit          = limit
        self.case_sensitive = case_sensitive
        self.options        = options
        self.parser         = parser
        self.gq2_handle     = None
        self.order_by       = order_by

        # The conditions string used in query (possibly uppercased). Appears in SQL-ish str(self) but not repr(self)
        self.conditions_for_exec = conditions

        if self.output not in (AS_TUPLE, AS_LIST, AS_DICT):
            raise GenQuery_Row_Return_Type_Error()

        if case_sensitive:
            self.options   &= ~(Option.UPPER_CASE_WHERE)
        else:
            self.options   |= Option.UPPER_CASE_WHERE

        self.gqi = None  # genquery inp
        self.gqo = None  # genquery out
        self.cti = None  # continue index

        # Filled when calling total_rows() on the Query.
        self._total = None

    def __repr__(self, **kw):
        return "Query(\n\t" + ",\n\t".join(
            name + "=" + ( repr(getattr(self,name)) if name != 'output' else self.output.__name__ )
            for name in self.__parameter_names
        ) + "\n)"

    @property
    def parameters(self): return dict((name,getattr(self,name)) for name in self.__parameter_names)

    def copy(self,**options):
        incorrect = list(k for k in options if k not in self.__parameter_names)
        if incorrect:
            raise GenQuery_Options_Spec_Error('Incorrect option(s) to Query: '+', '.join(incorrect))
        # let `options' override but not duplicate `self.parameters' in the keyword argument list
        keyword_items_list = list(self.parameters.items())  + list(options.items())
        return Query(self.callback, **dict(keyword_items_list))
        #return Query(self.callback, **{**self.parameters, **options})

    def exec_if_not_yet_execed(self):
        """Query execution is delayed until the first result or total row count is requested."""
        if self.parser == Parser.GENQUERY2:
            # The presence of a GenQuery2 handle indicates the query has already been executed.
            # Therefore, the results are already available for processing. If the query must be
            # executed again, a new Query object must be used.
            if self.gq2_handle is not None:
                return

            query_string = f'select {", ".join(self.columns)}'

            if self.conditions:        query_string += f' where {self.conditions}'
            if self.order_by:          query_string += f' order by {self.order_by}'
            if self.offset > 0:        query_string += f' offset {self.offset}'
            if self.limit is not None: query_string += f' limit {self.limit}'

            ret = self.callback.msi_genquery2_execute('', query_string)
            self.gq2_handle = ret['arguments'][0]
            return

        if self.gqi is not None:
            return
        if self.options & Option.UPPER_CASE_WHERE:
            # Uppercase the entire condition string. Should cause no problems,
            # since query keywords are case insensitive as well.
            self.conditions_for_exec = self.conditions.upper()
        import irods_types
        self.gqi = self.callback.msiMakeGenQuery(', '.join(self.columns),
                                                 self.conditions_for_exec,
                                                 irods_types.GenQueryInp())['arguments'][2]
        if self.offset > 0:
            self.gqi.rowOffset = self.offset
        else:
            # If offset is 0, we can (relatively) cheaply let iRODS count rows.
            # - with non-zero offset, the query must be executed twice if the
            #   row count is needed (see total_rows()).
            self.options |= Option.RETURN_TOTAL_ROW_COUNT

        if self.limit is not None and self.limit < MAX_SQL_ROWS - 1:
            # We try to limit the amount of rows we pull in, however in order
            # to close the query, 256 more rows will (if available) be fetched
            # regardless.
            self.gqi.maxRows = self.limit

        self.gqi.options |= self.options

        self.gqo    = self.callback.msiExecGenQuery(self.gqi, irods_types.GenQueryOut())['arguments'][1]
        self.cti    = self.gqo.continueInx
        self._total = None

    def total_rows(self):
        """Returns the total amount of rows matching the query.

        This includes rows that are omitted from the result due to limit/offset parameters.

        GenQuery2 does not automatically count the total number of rows in a resultset. Users
        are expected to execute another query to determine that. For that reason, this function
        always returns None when GenQuery2 is used.
        """
        if self._total is None:
            if self.parser == Parser.GENQUERY1:
                if self.offset == 0 and self.options & Option.RETURN_TOTAL_ROW_COUNT:
                    # Easy mode: Extract row count from gqo.
                    self.exec_if_not_yet_execed()
                    self._total = self.gqo.totalRowCount
                else:
                    # Hard mode: for some reason, using PostgreSQL, you cannot get
                    # the total row count when an offset is supplied.
                    # When RETURN_TOTAL_ROW_COUNT is set in combination with a
                    # non-zero offset, iRODS solves this by executing the query
                    # twice[1], one time with no offset to get the row count.
                    # Apparently this does not work (we get the correct row count, but no rows).
                    # So instead, we run the query twice manually. This should
                    # perform only slightly worse.
                    # [1]: https://github.com/irods/irods/blob/4.2.6/plugins/database/src/general_query.cpp#L2393
                    self._total = Query(self.callback, self.columns, self.conditions, limit=0,
                                        options=self.options|Option.RETURN_TOTAL_ROW_COUNT).total_rows()

        return self._total

    def __iter__(self):
        self.exec_if_not_yet_execed()

        if self.parser == Parser.GENQUERY2:
            column_count = len(self.columns)

            while True:
                try:
                    self.callback.msi_genquery2_next_row(self.gq2_handle)
                except RuntimeError as e:
                    if _END_OF_RESULTSET_ERROR_STRING_PART in str(e):
                        break
                    raise

                row = []
                for c in range(column_count):
                    ret = self.callback.msi_genquery2_column(self.gq2_handle, str(c), '')
                    row.append(ret['arguments'][2])

                try:
                    yield row
                except GeneratorExit:
                    self._close()
                    return

            return

        row_i = 0

        # Iterate until all rows are fetched / the query is aborted.
        while True:
            try:
                # Iterate over a set of rows.
                for r in range(self.gqo.rowCnt):
                    if self.limit is not None and row_i >= self.limit:
                        self._close()
                        return

                    row = [self.gqo.sqlResult[c].row(r) for c in range(len(self.columns))]
                    row_i += 1

                    if self.output == AS_TUPLE:
                        yield row[0] if len(self.columns) == 1 else tuple(row)
                    elif self.output == AS_LIST:
                        yield row
                    else:
                        yield OrderedDict(zip(self.columns, row))

            except GeneratorExit:
                self._close()
                return

            if self.cti <= 0 or self.limit is not None and row_i >= self.limit:
                self._close()
                return

            self._fetch()

    def _fetch(self):
        """Fetch the next batch of results.

        Not supported by GenQuery2.
        """
        if self.parser == Parser.GENQUERY2:
            # GenQuery2 always returns the full resultset. For that reason, this
            # function is a no-op. Care must be taken to not exhaust the server's
            # available memory.
            return

        ret      = self.callback.msiGetMoreRows(self.gqi, self.gqo, 0)
        self.gqo = ret['arguments'][1]
        self.cti = ret['arguments'][2]

    def _close(self):
        """Close the query.

        When GenQuery1 is used, prevents filling the statement table.
        When GenQuery2 is used, closes the resultset.
        """
        if self.parser == Parser.GENQUERY2:
            if self.gq2_handle is not None:
                self.callback.msi_genquery2_free(self.gq2_handle)
                self.gq2_handle = None
                self.order_by = ''
            return

        if not self.cti:
            return

        # msiCloseGenQuery fails with internal errors.
        # Close the query using msiGetMoreRows instead.
        # This is less than ideal, because it may fetch 256 more rows
        # (gqi.maxRows is overwritten) resulting in unnecessary processing
        # work. However there appears to be no other way to close the query.

        while self.cti > 0:
            # Close query immediately after getting the next batch.
            # This avoids having to soak up all remaining results.
            self.gqi.options |= Option.AUTO_CLOSE
            self._fetch()

        # Mark self as closed.
        self.gqi = None
        self.gqo = None
        self.cti = None

    def close(self):
        """Close the query.

        When GenQuery1 is used, prevents filling the statement table.
        When GenQuery2 is used, closes the resultset.
        """
        self._close()

    def first(self):
        """Get exactly one result (or None if no results are available)."""
        result = None
        for x in self:
            result = x
            break
        self._close()
        return result

    def __str__(self):
        projections = ', '.join(self.columns)
        where_clause = ' where ' + self.conditions_for_exec if self.conditions_for_exec else ''
        order_by_clause = ' order by ' + self.order_by if self.order_by else ''
        limit_clause = ' limit ' + str(self.limit) if self.limit is not None else ''
        offset_clause = ' offset '+ str(self.offset) if self.offset else ''
        return f'select {projections}{where_clause}{order_by_clause}{limit_clause}{offset_clause} [parser={self.parser.name}]'

    def __del__(self):
        """Auto-close query when Query goes out of scope."""
        self._close()


# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
# :::::               row-at-a-time query iterator               :::::
# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::


def row_iterator(  columns,     # comma-separated string, or list, of columns
                   conditions,  # genquery condition eg. "COLL_NAME not like '%/trash/%'"
                   row_return,  # AS_DICT or AS_LIST to specify rows as Python 'list's or 'dict's
                   callback     # fed in directly from rule call argument
                ):
    #
    #  now returns a Python class instance iterator
    #
    return Query(callback, columns, conditions, output=row_return)


def row_generator (columns, conditions, row_return, callback):

    #-=-=-=-=-=-  generator-style iterator -=-=-=-=-=-
    #
    # # - For reverse compatibility with the previous genquery.py
    # # - (eg if you rely on the 'next' built-in):
    # from genquery import *
    # from genquery import (row_generator as row_iterator)
    #
    return (row for row in row_iterator(columns, conditions, row_return, callback))


# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
# ::              Page-at-a-time query iterator                       ::
# ::-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=::
# ::::: --> Yields one page of 0<N<=MAX_SQL_ROWS results as a list  ::::
# :::::     of rows. As with the row_iterator, each row is either a ::::
#::::::     list or dict object (dictated by the row_return param)  ::::
# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

class paged_iterator (object):

    def __init__(self, columns, conditions, row_return, callback,
                 N_rows_per_page = MAX_SQL_ROWS):

        self.callback = callback
        self.set_rows_per_page ( N_rows_per_page , hard_limit_at_default = True )
        self.generator = row_generator (columns, conditions, row_return, callback )

    def __iter__(self): return self
    def __next__(self): return self.next()

    def set_rows_per_page (self, rpp, hard_limit_at_default = False):

        self.rows_per_page = max (1, int(rpp))

        if hard_limit_at_default:
            self.rows_per_page = min( self.rows_per_page,  MAX_SQL_ROWS )

        return self

    def next_arbitrary_size(self):

        this_page = list(itertools.islice(self.generator, self.rows_per_page))
        if 0 == len(this_page) : raise StopIteration
        return this_page

    def next(self):

        if self.rows_per_page > MAX_SQL_ROWS:

            return self.next_arbitrary_size()

        else:

            results = list( range(self.rows_per_page) )
            j = 0

            try:
                while j < len(results):
                    results[j] = next(self.generator)
                    j += 1
            except StopIteration:
                if j == 0: raise
            except Exception as e:
                self.callback.writeLine('serverLog',
                  '*** UNEXPECTED Exception in genquery.py paged iterate [next()]')
                self.callback.writeLine('serverLog','***   {!r}'.format(e))
                raise

            # -- truncate list size to fit the generated results
            del results[j:]

            return results

def logPrinter(callback, stream):
  return lambda logMsg : callback.writeLine( stream, logMsg )

#
#  Test rule below takes (like_path_rhs , requested_rowcount) as arguments via "rule_args" param
#
#    like_path_rhs :         {collection}/{dataname_sql_pattern} eg: '/myZone/home/alice/%'
#    requested_rowcount :    '<integer>' (for #rows-per-page) or '' (use generator)
#    detail logging stream : one of [ 'serverLog', 'stdout', 'stderr', '' ]
#

def test_python_RE_genquery_iterators( rule_args , callback, rei ):

    cond_like_RHS = rule_args[0]
    coll_name = "/".join(cond_like_RHS.split('/')[:-1])
    data_obj_name_pattern = cond_like_RHS.split('/')[-1]

    requested_rowcount = str( rule_args[1] )

    if rule_args[2:] and rule_args[2]:
        logger = logPrinter(callback, rule_args[2])
    else:
        logger = lambda logMsg: None

    conditions = "COLL_NAME = '{0}' AND DATA_NAME like '{1}'".format(
                  coll_name , data_obj_name_pattern )
    nr = 0
    n = 0

    last_page = None

    lists_generated  = 0
    lists_remainder = 0

    if len(requested_rowcount) > 0:

        rows_per_page = int(requested_rowcount)

        logger( "#\n#\n__query: (%d) rows at a time__"%(rows_per_page,) )
        for page in paged_iterator("DATA_NAME,DATA_SIZE" , conditions, AS_DICT, callback,
                                                         N_rows_per_page = rows_per_page ):
            i = 0
            logger("__new_page_from_query__")
            for row in page:

                logger( ("n={0},i={1} ; name = {2} ; size = {3}"
                        ).format(n, i, row['DATA_NAME'], row['DATA_SIZE'] ))
                n += 1
                i += 1
                nr += 1

            lists_generated += 1
            last_page = page

    else:

        logger( "__gen_mode_return_all_rows_from_query__" )

        for row in row_iterator( "DATA_NAME,DATA_SIZE" , conditions, AS_DICT, callback ):

            logger( ("n = {0} ; name = {1} ; size = {2}"
                          ).format( n, row['DATA_NAME'], row['DATA_SIZE'] ))
            n += 1
            nr += 1

    if lists_generated > 0:
        lists_remainder = len( last_page )

    rule_args[0] = str( nr ) # -> fetched rows total
    rule_args[1] = str( lists_generated )
    rule_args[2] = str( lists_remainder )
