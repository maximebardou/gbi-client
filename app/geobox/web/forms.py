# This file is part of the GBI project.
# Copyright (C) 2012 Omniscale GmbH & Co. KG <http://omniscale.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import werkzeug.datastructures
import re

from jinja2 import Markup

from flask import request, session, current_app, g

from wtforms.fields import HiddenField, TextField, SelectField, \
    PasswordField, BooleanField, FileField, TextAreaField
from wtforms.validators import Required, ValidationError, Optional, Regexp

from wtforms.ext.csrf.session import SessionSecureForm
from wtforms.ext.sqlalchemy.fields import QuerySelectField

from flaskext.babel import lazy_gettext, gettext, ngettext

from sqlalchemy.sql import functions
from geobox.model import (
    LocalWMTSSource, ExternalWMTSSource, ExternalWFSSource, Project, GBIServer, ParcelSearchSource
)


class BabelTranslations(object):
    def gettext(self, string):
        return gettext(string)

    def ngettext(self, singular, pluaral, n):
        return ngettext(singular, pluaral, n)

babel = BabelTranslations()

class _Auto():
    '''Placeholder for unspecified variables that should be set to defaults.

    Used when None is a valid option and should not be replaced by a default.
    '''
    pass


class SRSSelectField(SelectField):
    def __init__(self, default_choice=None, **kwargs):
        super(SRSSelectField, self).__init__(**kwargs)
        self.preselect = None

    def __call__(self, **kwargs):
        # allow preselecting default option
        if self.default:
            self.preselect = self.default
        # overwrite when data present
        if self.data:
            self.preselect = self.data
        return super(SRSSelectField, self).__call__(**kwargs)

    def iter_choices(self):
        for value, label, help_text in self.choices:
            selected = (
                self.preselect is not None and
                self.coerce(value) == self.preselect
            )
            yield (
                value,
                label,
                selected
            )

    def pre_validate(self, form):
        for v, _, help_text in self.choices:
            if self.data == v:
                break
        else:
            raise ValueError(self.gettext('Not a valid choice'))

# Form class from Flask-WTF 0.8 extension
class Form(SessionSecureForm):

    """
    Flask-specific subclass of WTForms **SessionSecureForm** class.

    Flask-specific behaviors:
    If formdata is not specified, this will use flask.request.form. Explicitly
      pass formdata = None to prevent this.

    csrf_context - a session or dict-like object to use when making CSRF tokens.
      Default: flask.session.

    secret_key - a secret key for building CSRF tokens. If this isn't specified,
      the form will take the first of these that is defined:
        * the SECRET_KEY attribute on this class
        * the value of flask.current_app.config["SECRET_KEY"]
        * the session's secret_key
      If none of these are set, raise an exception.

    csrf_enabled - whether to use CSRF protection. If False, all csrf behavior
      is suppressed. Default: check app.config for CSRF_ENABLED, else True

    """
    def __init__(self, formdata=_Auto, obj=None, prefix='', csrf_context=None,
                 secret_key=None, csrf_enabled=None, *args, **kwargs):

        if csrf_enabled is None:
            csrf_enabled = current_app.config.get('CSRF_ENABLED', True)
        self.csrf_enabled = csrf_enabled

        if formdata is _Auto:
            if self.is_submitted():
                formdata = request.form
                if request.files:
                    formdata = formdata.copy()
                    formdata.update(request.files)
                elif request.json:
                    formdata = werkzeug.datastructures.MultiDict(request.json);
            else:
                formdata = None
        if self.csrf_enabled:
            if csrf_context is None:
                csrf_context = session
            if secret_key is None:
                # It wasn't passed in, check if the class has a SECRET_KEY set
                secret_key = getattr(self, "SECRET_KEY", None)
            if secret_key is None:
                # It wasn't on the class, check the application config
                secret_key = current_app.config.get("SECRET_KEY")
            if secret_key is None and session:
                # It's not there either! Is there a session secret key if we can
                secret_key = session.secret_key
            if secret_key is None:
                # It wasn't anywhere. This is an error.
                raise Exception('Must provide secret_key to use csrf.')

            self.SECRET_KEY = secret_key
        else:
            csrf_context = {}
            self.SECRET_KEY = ""
        super(Form, self).__init__(formdata, obj, prefix, csrf_context=csrf_context, *args, **kwargs)

    def generate_csrf_token(self, csrf_context=None):
        if not self.csrf_enabled:
            return None
        return super(Form, self).generate_csrf_token(csrf_context)

    def validate_csrf_token(self, field):
        if not self.csrf_enabled:
            return
        super(Form, self).validate_csrf_token(field)

    def is_submitted(self):
        """
        Checks if form has been submitted. The default case is if the HTTP
        method is **PUT** or **POST**.
        """

        return request and request.method in ("PUT", "POST")

    def hidden_tag(self, *fields):
        """
        Wraps hidden fields in a hidden DIV tag, in order to keep XHTML
        compliance.

        .. versionadded:: 0.3

        :param fields: list of hidden field names. If not provided will render
                       all hidden fields, including the CSRF field.
        """

        if not fields:
            fields = [f for f in self if isinstance(f, HiddenField)]

        rv = [u'<div style="display:none;">']
        for field in fields:
            if isinstance(field, basestring):
                field = getattr(self, field)
            rv.append(unicode(field))
        rv.append(u"</div>")

        return Markup(u"".join(rv))

    def validate_on_submit(self):
        """
        Checks if form has been submitted and if so runs validate. This is
        a shortcut, equivalent to ``form.is_submitted() and form.validate()``
        """
        return self.is_submitted() and self.validate()

    def _get_translations(self):
        return babel

class ProjectEdit(Form):
    title = TextField(lazy_gettext('title'), validators=[Required()])
    start = HiddenField(default=0)

    def validate_download_level_end(form, field):
        if form.data['download_level_start'] > field.data:
            raise ValidationError(lazy_gettext('level needs to be bigger or equal to start level'))


def get_local_wmts_source():
    return g.db.query(LocalWMTSSource).all()

def get_external_wmts_source():
    return g.db.query(ExternalWMTSSource).filter_by(active=True).all()

def get_all_projects_withs_coverages():
    query = g.db.query(Project).with_polymorphic('*').filter(Project.coverage!=None)
    return query.filter(Project.coverage!='').all()

def get_wfs_source():
    return g.db.query(ExternalWFSSource).filter_by(active=True).all()


from sqlalchemy.sql import literal_column


def get_server_search_sources():
    return g.db.query(
        ExternalWFSSource.name.op('||')(literal_column("' ('")).op('||')(ExternalWFSSource.search_property).op('||')(literal_column("')'")).label('label'),
        literal_column("'wfs_'").op('||')
        (ExternalWFSSource.name).label('value')
    ).filter_by(active=True).union_all(g.db.query(
        GBIServer.title.label('label'),
        literal_column("'parcel_'").op('||')
        (ParcelSearchSource.id).label('value')
    ).filter(ParcelSearchSource.active==True).join(ParcelSearchSource.gbi_server)).all()


def get_gbi_servers():
    return g.db.query(GBIServer).all()

class SelectCoverage(Form):
    select_coverage = QuerySelectField(lazy_gettext('select coverage'), query_factory=get_all_projects_withs_coverages, get_label='title')

class SelectCouchLayers(Form):
    select_couch = SelectField(lazy_gettext('select couch'), choices=[],)


def raster_source_label(external_source):
    server_title = lazy_gettext('local')
    if external_source.gbi_server is not None:
        server_title = external_source.gbi_server.title

    return '%s (%s)' % (
        external_source.title,
        server_title
    )


class ImportProjectEdit(ProjectEdit):
    start_level = SelectField(lazy_gettext('start level'), coerce=int)
    end_level = SelectField(lazy_gettext('end level'), coerce=int)
    raster_source = QuerySelectField(
        lazy_gettext('raster_source'),
        query_factory=get_external_wmts_source,
        get_label=raster_source_label)
    update_tiles = BooleanField(lazy_gettext('update_tiles'))
    coverage = HiddenField()
    download_size = HiddenField()


class ExportProjectEdit(ProjectEdit):
    format = SelectField(lazy_gettext('format'), choices=[('MBTiles', 'MBTiles'), ('GTiff', 'TIFF'), ('JPEG', 'JPEG'), ('CouchDB', 'CouchDB')], coerce=str)
    srs = SRSSelectField(lazy_gettext('srs'), validators=[Optional()],
                         default='EPSG:25832')
    start_level = SelectField(lazy_gettext('start level'), coerce=int, validators=[Optional()])
    end_level = SelectField(lazy_gettext('end level'), coerce=int, validators=[Optional()])
    raster_source = QuerySelectField(lazy_gettext('raster_source'),query_factory=get_local_wmts_source, get_label='wmts_source.title', validators=[Optional()])
    raster_layers = HiddenField(validators=[Optional()])
    coverage = HiddenField(validators=[Optional()])
    download_size = HiddenField()


class ImportGeoJSONEdit(Form):
    file_name = SelectField(lazy_gettext('geojson file name'),
                            validators=[Required()])
    layers = SelectField(lazy_gettext('select existing layer'),
                         validators=[Optional()])
    name = TextField(lazy_gettext('new layer'), validators=[Optional()])


class ImportGMLEdit(Form):
    file_name = SelectField(lazy_gettext('gml file name'),
                            validators=[Required()])
    layers = SelectField(lazy_gettext('select existing layer'),
                         validators=[Optional()])
    name = TextField(lazy_gettext('new layer'), validators=[Optional()])
    srs = SRSSelectField(lazy_gettext('srs'), validators=[Required()],
                         default='EPSG:25832')


class ImportVectorEdit(Form):
    file_name = SelectField(lazy_gettext('file name'), validators=[Required()])
    srs = SRSSelectField(lazy_gettext('srs'), validators=[Required()],
                         default='EPSG:25832')
    layers = SelectField(lazy_gettext('select existing layer'), validators=[Optional()])
    name = TextField(lazy_gettext('new layer'), validators=[Optional()])

class TileBoxPathForm(Form):
    path = TextField(lazy_gettext('path'), validators=[])

class ExportVectorForm(Form):
    name = HiddenField(lazy_gettext('name'), validators=[Required()])
    geojson = HiddenField(lazy_gettext('geojson'))
    filename = TextField(lazy_gettext('filename'), validators=[Required()])
    export_type = SelectField(lazy_gettext('export_type'), choices=[('shp', 'SHP'), ('geojson', 'GeoJSON'), ('odata', 'OData')], coerce=str, validators=[Required()])
    srs = SRSSelectField(lazy_gettext('srs'), validators=[Optional()],
                         default='EPSG:25832')
    destination = SelectField(lazy_gettext('destination'), validators=[Required()])
    odata_url = TextField(lazy_gettext('odata_url'))

class UploadForm(Form):
    file = FileField(lazy_gettext('file'), validators=[Required()])
    overwrite = HiddenField('overwrite', default=False)

class UnlockRasterSourceForm(Form):
    username = TextField(lazy_gettext('rastersource_username'))
    password = PasswordField(lazy_gettext('rastersource_password'))

class RasterSourceForm(Form):
    url = TextField(lazy_gettext('rastersource_url'), validators=[Required()])
    username = TextField(lazy_gettext('rastersource_username'))
    password = PasswordField(lazy_gettext('rastersource_password'))
    name = TextField(lazy_gettext('rastersource_name'), validators=[Required(), Regexp('[a-z0-9_-]+$')])
    title = TextField(lazy_gettext('rastersource_title'), validators=[Required()])
    format = SelectField(lazy_gettext('rastersource_format'), validators=[Required()], choices=[('png', 'png'), ('jpeg', 'jpeg')])
    llbbox = TextAreaField(lazy_gettext('rastersource_llbox'), validators=[Required()])

class WMSForm(RasterSourceForm):
    srs = TextField(lazy_gettext('rastersource_srs_input'))
    layer = TextField(lazy_gettext('rastersource_layer_input'))
    version = SelectField(lazy_gettext('wms_version'), choices=[('1.1.1', '1.1.1'), ('1.3.0', '1.3.0')],
        validators=[Required()])



class ServerSearchForm(Form):
    search_source=QuerySelectField(lazy_gettext('source'),
                                   query_factory=get_server_search_sources,
                                   get_label=lambda a: a.label,
                                   get_pk=lambda a: a.value)


class CreateCouchAppForm(Form):
    couch_url = TextField(lazy_gettext('couchapp_couch_url'), validators=[Required()])


class SetGBIServerForm(Form):
    url = SelectField(lazy_gettext('gbi server'))
    username = TextField(lazy_gettext('username'))
    password = PasswordField(lazy_gettext('Password'))
    next = HiddenField(default='main.index')


class AddGBIServerForm(Form):
    title = TextField(lazy_gettext('title'), validators=[Required()])
    url = TextField(lazy_gettext('gbi server context document'), validators=[Required()])
    next = HiddenField(default='main.index')


class GMLUploadForm(Form):
    upload_file = FileField(lazy_gettext('Choose gml'),
                            validators=[Required()])
    srs = SRSSelectField(label=lazy_gettext('srs'),
                         validators=[Required()],
                         default='EPSG:25832')

    def validate_upload_file(form, field):
        if field.data and re.match('.*\.xml', field.data.filename) is None:
            raise ValidationError(lazy_gettext('only xml files allowed'))
