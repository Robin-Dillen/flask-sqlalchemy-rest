import json
try:
    from collections import Iterable
except ImportError:
    from collections.abc import Iterable

from dateutil.parser import parse
from flask import request, jsonify, current_app
from flask.views import MethodView
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql import sqltypes
from sqlalchemy import func
from sqlalchemy.engine import Row


class RestModel(MethodView):

    def __init__(self, db, model, ignore_columns=[], json_columns=[], search_columns=[], join_models={},
                 max_page_size=100, deleted_column_key=None, get_decorator=None, post_decorator=None, put_decorator=None,
                 delete_decorator=None, ignore_duplicates=False):
        self.db = db
        self.model = model
        self.ignore_columns = ignore_columns
        self.json_columns = json_columns
        self.search_columns = search_columns
        self.join_models = join_models
        self.max_page_size = max_page_size
        self.deleted_column_key = deleted_column_key
        # decorate the methods so the users can choose when to execute their code
        self.get = self.get if get_decorator is None else get_decorator(self.get)
        self.post = self.post if post_decorator is None else post_decorator(self.post)
        self.put = self.put if put_decorator is None else put_decorator(self.put)
        self.delete = self.delete if delete_decorator is None else delete_decorator(self.delete)
        self.ignore_duplicates = ignore_duplicates

    def get(self, id=None):
        if id is None:
            return self.query_all()
        else:
            obj = self.model.query.get(id.split(","))
            if obj:
                return self._resp(data=self._to_dict(obj))
            return self._resp(code=404, msg='obj not found')

    def delete(self, id):
        obj = self.model.query.get(id.split(","))
        if obj:
            if self.deleted_column_key is not None and hasattr(obj, self.deleted_column_key):
                setattr(obj, self.deleted_column_key, True)
            else:
                self.db.session.delete(obj)
            self.db.session.commit()
        return self._resp()

    def post(self):
        if request.is_json:
            try:
                response = []
                for index, entry in enumerate(list(request.json if type(request.json) is list else [request.json])):
                    obj = self.model()
                    if err_msg := self._verify_params(obj, entry):
                        return self._resp(code=400, msg=f"entry no. {index}: {err_msg}")
                    obj = self._update_model_from_dict(obj, entry)
                    if obj.id is not None and self.model.query.get(str(obj.id).replace(" ", "").split(",")) is not None:
                        if self.ignore_duplicates:
                            continue
                        else:
                            return self._resp(code=409, msg=f"entry no. {index}: duplicate primary key")
                    self.db.session.add(obj)
                    response.append(obj)
                self.db.session.commit()
                response = {'id': obj.id for obj in response}
                return self._resp(data=response)
            except Exception as e:
                current_app.logger.error(str(e))
                return self._resp(code=400, msg='invalid data')
        return self._resp(code=400, msg='invalid json')

    def put(self, id):
        if request.is_json:
            obj = self.model.query.get(id.split(","))
            if obj:
                try:
                    err_msg = self._verify_params(obj, request.json)
                    if err_msg:
                        return self._resp(code=400, msg=err_msg)
                    obj = self._update_model_from_dict(obj, request.json)
                    self.db.session.commit()
                    return self._resp(data={"id": obj.id})
                except Exception as e:
                    current_app.logger.error(str(e))
                    return self._resp(code=400, msg='invalid data')
            else:
                return self._resp(code=404, msg='obj not found')
        return self._resp(code=400, msg='invalid json')

    def query_with_args(self):
        page = request.args.get('_page', 1)
        page = int(page) if isinstance(page, int) or page.isdigit() else 1
        page_size = request.args.get('_page_size', 10)
        page_size = int(page_size) if isinstance(page, int) or page_size.isdigit() else 10
        if self.max_page_size is not None and self.max_page_size > 0:
            page_size = min(page_size, self.max_page_size)
        sort = request.args.get('_sort')
        desc = request.args.get('_desc')
        search = request.args.get('_search')
        join = request.args.get('_join')

        query = self.model.query
        query = self._filter_with_join(query, join)
        query = self._filter_with_params(query, request.args)
        query = self._filter_with_search(query, search)
        query = self._filter_with_sort(query, sort, desc)
        total = query.count()
        rows = query.slice((page - 1) * page_size, page * page_size).all()

        list = []
        for row in rows:
            if isinstance(row, Row) and len(row) == 1:
                list.append(row[0])
            elif isinstance(row, Iterable) or isinstance(row, Row) and len(row) > 1:
                data = self._to_dict(row[0])
                if len(row) > 1 and row[1]:
                    data[row[1].__tablename__] = self._to_dict(row[1])
                list.append(data)
            else:
                list.append(self._to_dict(row))

        return {"list": list, "page": page, "page_size": page_size, "total": total}

    def query_all(self):
        ret = self.query_with_args()
        return self._resp(data=ret)

    def _filter_with_params(self, query, pramas):
        for k, v in pramas.items():
            if isinstance(k, str):
                ks = k.split(':')
                if hasattr(self.model, ks[0]):
                    col = getattr(self.model, ks[0])
                    v = self._str_to_date_time(type(col.type), v)
                    if len(ks) > 1:
                        v = None if v == 'null' else v
                        query = self._filter_with_operator(query, col, ks[1], v)
                    elif len(v) == 0:
                        query = query.with_entities(col)
                    else:
                        query = query.filter(col == v)
        return query

    def _filter_with_join(self, query, join_table):
        if join_table and join_table in self.join_models:
            if obj := self.join_models.get(join_table):
                model = obj.get('model')
                column_a = obj.get('column_a')
                column_b = obj.get('column_b')
                inner_join = obj.get('inner_join')
                if column_a and isinstance(column_a, InstrumentedAttribute) \
                            and column_b and isinstance(column_b, InstrumentedAttribute):
                    query = self.db.session.query(self.model, model)
                    if inner_join is True:
                        query = query.join(model, column_a == column_b)
                    else:
                        query = query.outerjoin(model, column_a == column_b)

        return query

    def _filter_with_search(self, query, search):
        if search and self.search_columns and self.db.engine.name != 'sqlite':
            if cols := [
                getattr(self.model, col_name)
                for col_name in self.search_columns
                if hasattr(self.model, col_name)
            ]:
                query = query.filter(self.db.func.concat_ws('_', *cols).contains(search))
        return query

    def _filter_with_sort(self, query, sort, desc):
        if sort and hasattr(self.model, sort):
            if desc and str(desc) == '1':
                query = query.order_by(getattr(self.model, sort).desc())
            else:
                query = query.order_by(getattr(self.model, sort))
        return query

    def _filter_with_operator(self, query, coloumn, operator, value):
        if operator == 'eq':
            query = query.filter(coloumn == value)
        elif operator == 'ne':
            query = query.filter(coloumn != value)
        elif operator == 'gt':
            query = query.filter(coloumn > value)
        elif operator == 'ge':
            query = query.filter(coloumn >= value)
        elif operator == 'lt':
            query = query.filter(coloumn < value)
        elif operator == 'le':
            query = query.filter(coloumn <= value)
        elif operator == 'in':
            query = query.filter(coloumn.in_(value.split(',')))
        elif operator == 'ni':
            query = query.filter(coloumn.notin_(value.split(',')))
        elif operator == 'ct':
            query = query.filter(coloumn.contains(func.ilike(value)))
        elif operator == 'Ct':
            query = query.filter(coloumn.contains(value))
        elif operator == 'nc':
            query = query.filter(~coloumn.contains(func.ilike(value)))
        elif operator == 'Nc':
            query = query.filter(~coloumn.contains(func.like(value)))
        elif operator == 'sw':
            query = query.filter(coloumn.ilike(f'{str(value)}%'))
        elif operator == 'Sw':
            query = query.filter(coloumn.like(f'{str(value)}%'))
        elif operator == 'ew':
            query = query.filter(coloumn.ilike(f'%{str(value)}'))
        elif operator == 'Ew':
            query = query.filter(coloumn.like(f'%{str(value)}'))
        elif operator == 'min':
            query = query.with_entities(func.min(coloumn))
        elif operator == 'max':
            query = query.with_entities(func.max(coloumn))
        elif operator == 'avg':
            query = query.with_entities(func.avg(coloumn))
        elif operator == "distinct":
            query = query.distinct(coloumn)
        return query

    def _to_dict(self, obj):
        ret = {}
        if obj:
            for column in obj.__table__.columns:
                if column.name not in self.ignore_columns:
                    value = getattr(obj, column.name)
                    if value and column.type.__class__ in [sqltypes.DateTime, sqltypes.Date, sqltypes.Time]:
                        ret[column.name] = str(value)
                    elif column.name in self.json_columns:
                        ret[column.name] = self._str_to_json(value)
                    else:
                        ret[column.name] = value
        return ret

    def _verify_params(self, obj, data):
        if not data or not isinstance(data, dict):
            return 'invalid json'
        for col in obj.__table__.columns:
            if not col.nullable and not col.primary_key:
                if data.get(col.name) is None and getattr(obj, col.name) is None:
                    return col.name + ' is required'
            if type(col.type) == sqltypes.Boolean and col.name in data:
                val = data.get(col.name)
                if str(val).lower() not in ['none', '1', '0', 'true', 'false', 'yes', 'no']:
                    return col.name + ' require boolean value'
        return None

    def _update_model_from_dict(self, obj, data):
        if data:
            for k, v in data.items():
                if hasattr(obj, k):
                    column_type = type(getattr(obj.__table__.columns, k).type)
                    if column_type in [sqltypes.DateTime, sqltypes.Date, sqltypes.Time]:
                        v = self._str_to_date_time(column_type, v)
                    if column_type == sqltypes.Boolean:
                        if str(v).lower() in ['1', 'true', 'yes']:
                            v = True
                        elif str(v).lower() in ['0', 'false', 'no']:
                            v = False
                        else:
                            v = None
                    if isinstance(v, dict) or isinstance(v, list):
                        v = json.dumps(v) if v else None
                    setattr(obj, k, v)
        return obj

    def _str_to_date_time(self, column_type, value):
        if value:
            if column_type == sqltypes.DateTime:
                value = parse(value)
            if column_type == sqltypes.Date:
                value = parse(value).date()
            if column_type == sqltypes.Time:
                value = parse(value).time()
        return value

    def _str_to_json(self, text):
        try:
            return json.loads(text)
        except:
            return text

    def _resp(self, code=200, msg="OK", data={}):
        return jsonify(code=code, msg=msg, data=data), code
