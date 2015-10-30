from __future__ import unicode_literals

import argparse
import imp
import itertools
import os
import re
from collections import defaultdict, namedtuple
from contextlib import contextmanager

from babelapi.data_type import (
    Float32,
    Float64,
    Int32,
    Int64,
    UInt32,
    UInt64,
    Void,
    is_binary_type,
    is_boolean_type,
    is_composite_type,
    is_float_type,
    is_integer_type,
    is_list_type,
    is_nullable_type,
    is_numeric_type,
    is_primitive_type,
    is_string_type,
    is_struct_type,
    is_tag_ref,
    is_timestamp_type,
    is_union_type,
    is_void_type,
)
from babelapi.generator import CodeGenerator

cmdline_desc = """\
Generate .NET project for Dropbox Api.
"""

_cmdline_parser = argparse.ArgumentParser(description=cmdline_desc)
_cmdline_parser.add_argument(
    '-l',
    '--link',
    action='store_true',
    help='Link instead of copy files',
)

try:
    from csproj import make_csproj_file
except ImportError:
    # The babel generate calls imp.load_source on this file, which precludes
    # referencing csproj as if it was part of a module, so we have to jump
    # through this hoop...
    csproj = os.path.join(os.path.dirname(__file__), 'csproj.py')
    csproj_module = imp.load_source('csproj_module', csproj)
    make_csproj_file = csproj_module.make_csproj_file


def memo_one(fn):
    """
    Memoize a single argument instance method.
    """
    cache = {}
    def wrapper(self, arg):
        value = cache.get(arg)
        if value is not None:
            return value
        value = fn(self, arg)
        cache[arg] = value
        return value
    return wrapper


ConstructorArg = namedtuple('ConstructorArg', ('type', 'name', 'arg', 'doc'))


class CSharpGenerator(CodeGenerator):
    DEFAULT_NAMESPACE = 'Dropbox.Api.'

    _CAMEL_CASE_RE = re.compile('((?<=[a-z0-9])[A-Z]|(?!^)[A-Z](?=[a-z]))')
    _CSHARP_KEYWORDS = frozenset({
        'abstract', 'add', 'alias', 'as', 'ascending', 'async', 'await',
        'base', 'bool', 'break', 'byte', 'case', 'catch', 'char', 'checked',
        'class', 'const', 'continue', 'decimal', 'default', 'delegate',
        'descending', 'do', 'double', 'dynamic', 'else', 'enum', 'event',
        'explicit', 'extern', 'false', 'finally', 'fixed', 'float', 'for',
        'foreach', 'from', 'get', 'global', 'goto', 'group', 'if', 'implicit',
        'in', 'int', 'interface', 'internal', 'into', 'is', 'join', 'let',
        'lock', 'long', 'namespace', 'new', 'null', 'object', 'operator',
        'orderby', 'out', 'override', 'params', 'partial', 'private',
        'protected', 'public', 'readonly', 'ref', 'remove', 'return', 'sbyte',
        'sealed', 'select', 'set', 'short', 'sizeof', 'stackalloc', 'static',
        'string', 'struct', 'switch', 'this', 'throw', 'true', 'try', 'typeof',
        'uint', 'ulong', 'unchecked', 'unsafe', 'ushort', 'using', 'value',
        'var', 'virtual', 'void', 'volatile', 'where', 'while', 'yield',
    })

    cmdline_parser = _cmdline_parser

    def __init__(self, *args, **kwargs):
        super(CSharpGenerator, self).__init__(*args, **kwargs)
        self._prefixes = []
        self._prefix = ''
        self._name_list = []
        self._prevent_collisions = set()
        self._generated_files = []
        self._tag_context = None

    def generate(self, api):        
        for namespace in api.namespaces.itervalues():
            self._compute_related_types(namespace)
            self._generate_namespace(namespace)

        self._generate_dropbox_client(api, 'DropboxClient', 'user')
        self._generate_dropbox_client(api, 'DropboxTeamClient', 'team')

        self._generate_xml_doc(api)
        self._generate_csproj()
        self._copy_common_files()

    @contextmanager
    def cs_block(self, **kwargs):
        """
        Context manager for an allman style block, which is more common
        style for c#
        """ 
        kwargs['allman'] = True
        with self.block(**kwargs):
            yield

    @contextmanager
    def region(self, label):
        """
        Context manager for a c# region. All code emitted within the context
        is within the region.

        Args:
            label (str): The region label
        """
        self.emit('#region {0}'.format(label))
        self.emit()
        yield
        self.emit()
        self.emit('#endregion')

    def if_(self, condition):
        """
        Context manager for an `if` statement. All code emitted within the context
        is within the if statement.

        Args:
            condition (str): The if condition
        """
            
        return self.cs_block(before='if ({0})'.format(condition))

    def else_(self):
        """
        Context manager for an else statement. All code emitted within the context
        is within the else statement.
        """
        return self.cs_block(before='else')

    def else_if(self, condition):
        """
        Context manager for an `else if` statement. All code emitted within the
        context is within the `else if` statement.

        Args:
            condition (str): The else if condition.
        """
        return self.cs_block(before='else if ({0})'.format(condition))

    def namespace(self, name):
        """
        Context manager for a `namespace` stement. All code emitted within the
        context is within the namespace.
        """
        return self.cs_block(before='namespace {0}{1}'.format(self.DEFAULT_NAMESPACE, name))

    def class_(self, name, inherits=None, access=''):
        """
        Context manager for a class. All code emitted within the context is part of
        the class.

        Args:
            name (str): The name of the class.
            inherits (str|iterable): The base types for the class, if any. If
                this is a string it is added to the code verbatim, if an 
                iterable, then joined with ', '
            access (str): The access modifierd of the class.
        """

        elements = []
        if access:
            elements.append(access)
        elements.append('class')
        elements.append(name)
        if inherits:
            elements.append(':')
            if isinstance(inherits, basestring):
                elements.append(inherits)
            else:
                elements.append(', '.join(inherits))
        return self.cs_block(before=' '.join(elements))

    def using(self, declaration):
        """
        Context manager for a `using` block. All code emitted within the context
        is within the using block.

        Args:
            declaration (str): The using declaration.
        """
        return self.cs_block(before='using ({0})'.format(declaration))

    def emit(self, text=''):
        """
        Wraps the regular generator emit() method. 

        This is used by the prefix() and doc_comment() methods to prepend a
        fixed string to each line emitted within those contexts.

        Args:
            text (str): The text to emit
        """
        if text and self._prefix:
            super(CSharpGenerator, self).emit(self._prefix + text)
        else:
            super(CSharpGenerator, self).emit(text)

    def output_to_relative_path(self, filename):
        """
        Wraps the regular generator output_to_relative_path() method.

        This is used to keep track of the set of all files that are generated.

        Args:
            filename (str): The name of the file to generate.
        """
        self._generated_files.append(filename)
        return super(CSharpGenerator, self).output_to_relative_path(filename)

    @contextmanager
    def prefix(self, prefix):
        """
        Context manager that prepends the supplied prefix to every line of text
        that is emitted within the context.

        Args:
            prefix (str): The prefix to prepend to every line.
        """
        self._prefixes.append(prefix)
        self._prefix = ''.join(self._prefixes)
        yield
        self._prefixes.pop()
        self._prefix = ''.join(self._prefixes)

    @contextmanager
    def doc_comment(self, data_type=None, is_constructor=False):
        """
        Context manager that treats all lines of text emitted within the
        context as part of a doc comment (i.e. prefixed with '///').

        Args:
            data_type (babelapi.data_type.DataType): The type for which this
                documentation is being generated. This helps resolve references
                in the _tag_handler method.
            is_constructor (bool): Indicated whether this doc comment if for
                a constructor - also used when resolving references.
        """
        self._tag_context = (data_type, is_constructor)
        with self.prefix('/// '):
            yield
        self._tag_context = None

    def auto_generated(self):
        """
        Generates a standard comment for the head of every file. This prevents
        StyleCop from measuring the contents of the file.
        """
        with self.prefix('// '):
            self.emit('<auto-generated>')
            self.emit('Auto-generated by BabelAPI, do not modify.')
            self.emit('</auto-generated>')
        self.emit()

    def emit_wrapped_text(self, s, **kwargs):
        """
        Wraps the regular generator emit_wrapped_text() method. 

        This does three things.
        1. It ensures consistend prefix behavior with the modified emit method
        2. It sets a default width of 95
        3. It calls self.process_doc on the input string if the process keyword
            is present

        Args:
            s (str): The string to emit and wrap.
            process (callable): The function to handle tags in the emitted text.
        """
        kwargs['prefix'] = self._prefix + kwargs.get('prefix', '')
        if 'width' not in kwargs:
            kwargs['width'] = 95
        if 'process' in kwargs:
            process = kwargs.pop('process')
            s = self.process_doc(s, process)

        super(CSharpGenerator, self).emit_wrapped_text(s, **kwargs)

    @contextmanager
    def switch(self, expression):
        """
        Context manager for a `switch` statement.

        Args:
            expression (str): The expression to switch on.
        """
        self.emit('switch ({0})'.format(expression))
        self.emit('{')
        yield
        self.emit('}')

    @contextmanager
    def case(self, constant=None, needs_break=True):
        """
        Context manager for a `case` statement.

        Args:
            constant (str): If this is not provided, then this is generated as
                the default case. 
            need_break (bool): Indicates whether a break statement should 
                automatically be appended with the case statement ends.
        """
        self.emit('case {0}:'.format(constant) if constant else 'default:')
        with self.indent():
            yield
            if needs_break:
                self.emit('break;')

    @contextmanager
    def _local_names(self, names):
        """
        This context manager is used to help resolve names if there are
        collisions between struct or union members and top level type names
        within the namespace.

        Args:
            names (iterable of str): The local names.
        """
        self._name_list.append(list(names))
        self._prevent_collisions = set(itertools.chain(*self._name_list))
        yield
        self._name_list.pop()
        self._prevent_collisions = set(itertools.chain(*self._name_list))

    def emit_xml(self, doc, tag, **attrs):
        """
        Emits an xml element.

        Args:
            doc (str): The contents of the xml element, if this is `None` then
                the element is emitted in self closed form
            tag (str): The xml element tag name.
            attrs (dict): The attributes (if any) for the elemen
        """
        tag_start = '<' + tag
        if attrs:
            tag_start += ' ' + ' '.join('{0}="{1}"'.format(k, v) for k,v in attrs.iteritems())

        if doc is None:
            self.emit(tag_start + ' />')
        else:
            self.emit_wrapped_text('{0}>{1}</{2}>'.format(tag_start, doc, tag),
                                   process=self._tag_handler)

    @contextmanager
    def xml_block(self, tag, **attrs):
        """
        Context manager that includes all emitted code within an xml element

        Args:
            tag (str): The xml element tag name
            attrs (dict): The xml element attributes, if any.
        """
        if attrs:
            attributes = ' '.join('{0}="{1}"'.format(k, v) for k,v in attrs.iteritems())
            self.emit('<{0} {1}>'.format(tag, attributes))
        else:
            self.emit('<{0}>'.format(tag))
        if self._prefixes:
            yield
        else:
            with self.indent():
                yield
        self.emit('</{0}>'.format(tag))

    def emit_summary(self, doc=""):
        """
        Emits the supplied documentation as a summary element.

        Args:
            doc (str): The documentation to emit, if this is multi-line, then
                each line is wrapped in a `para` element.
        """
        lines = doc.splitlines()
        if len(lines) > 0:
            with self.xml_block('summary'):
                for line in lines:
                    self.emit_xml(line, 'para')
        else:
            self.emit_xml(doc, 'summary')

    def emit_ctor_summary(self, class_name):
        self.emit_summary('Initializes a new instance of the <see cref="{0}" /> '
                          'class.'.format(class_name))

    def _tag_handler(self, tag, value):
        """
        Passed as to the process_doc() method to handle tags that are found in
        the documentation string

        Args:
            tag (str): The tag type, one of 'field|link|route|type|val'
            value (str): The value of the tag.
        """
        if tag == 'field':
            if '.' in value:
                parts = map(self._public_name, value.split('.'))
                return '<see cref="{0}" />'.format('.'.join(parts))
            elif self._tag_context:
                data_type, is_constructor = self._tag_context
                if is_constructor:
                    return '<paramref name="{0}" />'.format(self._arg_name(value))
                else:
                    return '<see cref="{0}" />'.format(self._public_name(value))
            else:
                return '<paramref name="{0}" />'.format(self._arg_name(value))
        elif tag == 'link':
            parts = value.split(' ')
            uri = parts[-1]
            text = ' '.join(parts[:-1])
            return '<a href="{0}">{1}</a>'.format(uri, text)
        elif tag == 'route':
            return ('<see cref="{0}{1}.Routes.{1}Routes.{2}Async" />'.format(
                    self.DEFAULT_NAMESPACE, self._ns, self._public_name(value)))
        elif tag == 'type':
            return '<see cref="{0}" />'.format(self._public_name(value))
        elif tag == 'val':
            return '<c>{0}</c>'.format(value.strip('`'))
        else:
            assert False, 'Unknown tag: {0}:{1}'.format(tag, value)

    def _typename(self, data_type, void=None, is_property=False):
        """
        Generates a C# type from a data_type

        The translations for the primitive types are the exact equivalent
        C# value types. For composite types, the type name is represented using
        CamelCase. The list type is handled slightly differently for the 
        property and constructor cases where it is an IList or IEnumerable
        respectively.j

        Args:
            data_type (babelapi.data_type.DataType): The type to translate.
            void (str): If supplied, this is the value to return if data_type
                is void.
            is_property (bool): Indicates whether the type translation is for
                a property type. Lists have different types expressed for
                properties than in other places.
        """
        if is_nullable_type(data_type):
            nullable = True
            data_type = data_type.data_type
        else:
            nullable = False

        name = data_type.name
        if is_composite_type(data_type):
            public = self._public_name(name)
            type_ns = self._public_name(data_type.namespace.name)
            if type_ns != self._ns:
                public = type_ns + '.' + public
            if public in self._prevent_collisions:
                return self.DEFAULT_NAMESPACE + self._ns + '.' + public
            return public
        elif is_list_type(data_type):
            if is_property:
                return 'col.IList<{0}>'.format(self._typename(data_type.data_type))
            else:
                return 'col.IEnumerable<{0}>'.format(self._typename(data_type.data_type))
        elif is_string_type(data_type):
            return 'string'
        elif is_binary_type(data_type):
            return 'byte[]'
        else:
            suffix = '?' if nullable else ''

            if is_boolean_type(data_type):
                typename = 'bool'
            elif isinstance(data_type, Int32):
                typename = 'int'
            elif isinstance(data_type, UInt32):
                typename = 'uint'
            elif isinstance(data_type, Int64):
                typename = 'long'
            elif isinstance(data_type, UInt64):
                typename = 'ulong'
            elif isinstance(data_type, Float32):
                typename = 'float'
            elif isinstance(data_type, Float64):
                typename = 'double'
            elif is_timestamp_type(data_type):
                typename = 'sys.DateTime'
            elif is_void_type(data_type):
                return void or 'void'
            else:
                assert False, 'Unknown data type %r' % data_type

            return typename + suffix

    def _process_literal(self, literal):
        """
        Translate literal values used in defaults

        Args:
            literal: The literal value.
        """
        if isinstance(literal, bool):
            return 'true' if literal else 'false'
        return literal

    def _type_literal_suffix(self, data_type):
        """
        Returns the suffix needed to make a numeric literal values type explicit.

        Args:
            data_type (babelapi.data_type.DataType): The type in question.
        """
        if not is_numeric_type(data_type) or isinstance(data_type, Int32):
            return ''
        elif isinstance(data_type, UInt32):
            return 'U'
        elif isinstance(data_type, Int64):
            return 'L'
        elif isinstance(data_type, UInt64):
            return 'UL'
        elif isinstance(data_type, Float32):
            return 'F'
        elif isinstance(data_type, Float64):
            return 'D'
        else:
            assert False, 'Unknown numeric data type %r' % data_type

    def _could_be_null(self, data_type):
        """
        Returns true if 'data_type' could be null, i.e. if it is not a value type

        Args:
            data_type (babelapi.data_type.DataType): The type in question.
        """
        return is_composite_type(data_type) or is_string_type(data_type) or is_list_type(data_type)

    def _verbatim_string(self, string):
        """
        Creates a C# verbatim string (way easier than dealing with escapes)

        Args:
            string (str): The string to represent.
        """
        return '@"{0}"'.format(string.replace('"', '""'))

    def _process_composite_default(self, field, include_null_check=True):
        """
        Generate code to initialize a default value for a composite field.

        Note: This is not implemented for fields that are structs.

        Args:
            field: (babelapi.data_type.Field): The field to initialize.
            include_null_check (bool): Indicates whether a check for an
                argument being null should be emitted.
        """
        if is_struct_type(field.data_type):
            raise NotImplementedError()
        elif is_union_type(field.data_type):
            self._process_union_default(field, include_null_check)
        else:
            assert False, 'field is neither struct nor union: {0}.'.format(field)

    def _process_union_default(self, field, include_null_check):
        """
        Generate code to initialize a default value for a field that is a union.

        Note: This only works for union fields that don't have arguments.

        Args:
            field: (babelapi.data_type.Field): The field to initialize.
            include_null_check (bool): Indicates whether a check for an
                argument being null should be emitted.
        """
        assert is_tag_ref(field.default), (
            'Default union value is not a tag ref: {0}'.format(field.default))

        union = field.default.union_data_type
        default = field.default.tag_name

        arg_name = (self._arg_name(field.name) if include_null_check else
                    'this.{0}'.format(self._public_name(field.name)))
      
        assign_default = '{0} = {1}.{2}.Instance;'.format(
            arg_name, self._public_name(union.name), self._public_name(default))

        if include_null_check: 
            with self.if_('{0} == null'.format(arg_name)):
                self.emit(assign_default)
        else:
            self.emit(assign_default)
    
    def _check_constraints(self, name, data_type, has_null_check):
        """
        Emits code to checks the validity of a field when constructing an
        object. 

        Args:
            name (str): The field name.
            data_type (babelapi.data_type.DataType): The type of the field
            has_null_check (bool): Indicates whether prior code has already
                generated a null check for this field - this happens if a
                composite field has a default.
        """
        if is_nullable_type(data_type):
            nullable = True
            data_type = data_type.data_type
        else:
            nullable = False

        checks = []
        if is_numeric_type(data_type):
            suffix = self._type_literal_suffix(data_type)
            if data_type.min_value is not None:
                checks.append('{0} < {1}{2}'.format(name, data_type.min_value, suffix))
            if data_type.max_value is not None:
                checks.append('{0} > {1}{2}'.format(name, data_type.max_value, suffix))
        elif is_string_type(data_type):
            if data_type.min_length is not None:
                checks.append('{0}.Length < {1}'.format(name, data_type.min_length))
            if data_type.max_length is not None:
                checks.append('{0}.Length > {1}'.format(name, data_type.max_length))
            if data_type.pattern is not None:
                verbatim_pattern = self._verbatim_string(data_type.pattern)
                checks.append('!re.Regex.IsMatch({0}, {1})'.format(name, verbatim_pattern))
        elif is_list_type(data_type):
            listName = name + 'List'
            element_type = self._typename(data_type.data_type)
            self.emit('var {0} = new col.List<{1}>({2} ?? new {1}[0]);'.format(
                    listName, element_type, name))
            self.emit()

            if data_type.min_items is not None:
                checks.append('{0}.Count < {1}'.format(listName, data_type.min_items))
            if data_type.max_items is not None:
                checks.append('{0}.Count > {1}'.format(listName, data_type.max_items))

        pre_check = post_check = ''
        control = self.if_
        need_emit = False

        if nullable:
            pre_check = '{0} != null && ('.format(name)
            post_check = ')'
        elif self._could_be_null(data_type):
            if not has_null_check:
                with self.if_('{0} == null'.format(name)):
                    self.emit('throw new sys.ArgumentNullException("{0}");'.format(name))
            need_emit = True
            control = self.else_if

        if checks:
            with control('{0}{1}{2}'.format(pre_check, ' || '.join(checks), post_check)):
                self.emit('throw new sys.ArgumentOutOfRangeException("{0}");'.format(name))
                need_emit = True

        if need_emit:
            self.emit()

    @memo_one
    def _segment_name(self, name):
        """
        Segments a name into a list of lowercase components.

        Names are segmented on '/' or '_' characters and also on CamelCase boundaries.

        Args:
            name (str): The name to segment.
        """
        name = name.replace('/', '_')
        name = CSharpGenerator._CAMEL_CASE_RE.sub(r'_\1', name).lower()
        return name.split('_')

    @memo_one
    def _public_name(self, name):
        """
        Creates an initial capitalize CamelCase representation of name.
    
        This performs the following transformations.
            foo_bar -> FooBar
            fooBar -> FooBar
            FooBar -> FooBar

        Args:
            name (str): The name to transform
        """
        return ''.join(x.capitalize() for x in self._segment_name(name))

    @memo_one
    def _arg_name(self, name):
        """
        Creates an initial lowercase camelCase representation of name.
    
        This performs the following transformations.
            foo_bar -> fooBar
            fooBar -> fooBar
            FooBar -> fooBar

        Args:
            name (str): The name to transform
        """
        public = self._public_name(name)
        arg_name = public[0].lower() + public[1:]
        if arg_name in CSharpGenerator._CSHARP_KEYWORDS:
            return '@' + arg_name
        return arg_name

    @memo_one
    def _name_words(self, name):
        """
        Creates a space separated sequence of words from a name.

        This performs the following transformation.
            foo_bar -> 'foo bar'
            fooBar -> 'foo bar'
            FooBar -> 'foo bar'

        Args:
            name (str): The name to transform
        """
        return ' '.join(self._segment_name(name))

    def _generate_xml_doc(self, api):
        """
        Generates an xml documentation file containing the namespace level
        documentation for the API specification being generated.

        Args:
            api (babelapi.api.Api): The API specification.
        """
        with self.output_to_relative_path('namespace_summaries.xml'):
            self.emit('<?xml version="1.0"?>')
            with self.xml_block('doc'):
                with self.xml_block('assembly'):
                    self.emit_xml('_NamespaceSummaries_', 'name')
                with self.xml_block('members'):
                    with self.xml_block('member', name='N:{0}'.format(self.DEFAULT_NAMESPACE[:-1])):
                        self.emit_summary('Contains the dropbox client - '
                                '<see cref="T:{0}DropboxClient"/>.'.format(self.DEFAULT_NAMESPACE))
                    for namespace in api.namespaces.itervalues():
                        ns_name = self._public_name(namespace.name)
                        with self.xml_block('member', name='N:{0}{1}'.format(
                                self.DEFAULT_NAMESPACE, ns_name)):
                            doc = namespace.doc or ('Contains the types used by the routes declared in '
                                                    '<see cref="T:{0}{1}.Routes.{1}Routes" />.'.format(
                                                        self.DEFAULT_NAMESPACE, ns_name))
                            self.emit_summary(doc)
                        with self.xml_block('member', name='N:{0}{1}.Routes'.format(
                                self.DEFAULT_NAMESPACE, ns_name)):
                            self.emit_summary('Contains the routes for the <see cref="N:{0}{1}" /> '
                                    'namespace.'.format(self.DEFAULT_NAMESPACE, ns_name))

    def _generate_csproj(self):
        """
        Generates two csproj files.

        One is a portable assembly - this is the assembly that is intended to
        be distributed; the other is a regular desktop .Net assembly that is
        used to generate documentation - the documentation tool SandCastle 
        cannot reliably generate documentation from a portable assembly.
        """
        files = [f for f in self._generated_files if f.endswith('.cs')]
        with self.output_to_relative_path('Dropbox.Api.csproj'):
            self.emit_raw(make_csproj_file(files, is_doc=False, link=self.args.link))
        with self.output_to_relative_path('Dropbox.Api.Doc.csproj'):
            self.emit_raw(make_csproj_file(files, is_doc=True, link=self.args.link))

    def _copy_common_files(self):
        """
        Copies all the files in the `common` subdirectory into the target dir.
        """
        common_dir = os.path.join(os.path.dirname(__file__), 'common')
        common_len = len(common_dir)
        for dirpath, _, filenames in os.walk(common_dir):
            for filename in filenames:
                src_path = os.path.join(dirpath, filename)
                dest_path = src_path[common_len + 1:]

                with self.output_to_relative_path(dest_path):
                    print("Copying {0}".format(dest_path))
                    with open(src_path, 'r') as f:
                        doc = f.read().decode('utf-8')
                        if not doc.endswith('\n'):
                            doc += '\n'
                        self.emit_raw(doc)
                            
    def _generate_dropbox_client(self, api, client_name, auth_type):
        """
        Generates a partial class for the DropboxClient, this only includes
        the route declarations and the route initialization, the rest of the
        class is in the file `common/DropboxClient.common.cs`

        Args:
            api (babelapi.api.Api): The API specification.
            client_name (str): The name of the client. e.g. DropboxClient, DropboxTeamClient
            auth_type (str): The expected auth type for the client. e.g. User, Team
        """
        def get_auth_type(ns):
            routes = ns.routes
            if not routes:
                return None
            return routes[0].attrs.get('auth', 'user')
            
        ns_names = [self._public_name(ns.name) for ns in api.namespaces.itervalues()
                    if get_auth_type(ns) == auth_type]

        with self.output_to_relative_path('{0}.cs'.format(client_name)):
            self.auto_generated()
            with self.cs_block(before='namespace {}'.format(self.DEFAULT_NAMESPACE[:-1])):
                self.emit('using sys = System;')
                self.emit()
                self.emit('using Dropbox.Api.Babel;')
                for ns_name in ns_names:
                    self.emit('using {0}{1}.Routes;'.format(self.DEFAULT_NAMESPACE, ns_name))
                self.emit()

                with self.class_(client_name, access='public sealed partial'):
                    first = True
                    for ns_name in ns_names:
                        if first:
                            first = False
                        else:
                            self.emit()

                        with self.doc_comment():
                            self.emit_summary('Gets the {0} routes.'.format(ns_name))
                        self.emit('public {0}Routes {0} {{ get; private set; }}'.format(ns_name))

                    self.emit()
                    with self.doc_comment():
                        self.emit_summary('Initializes the routes.')
                        self.emit_xml('The transport.', 'returns')
                    with self.cs_block(before='private void InitializeRoutes(ITransport transport)'):
                        for ns_name in ns_names:
                            self.emit('this.{0} = new {0}Routes(transport);'.format(ns_name))

    def _compute_related_types(self, ns): 
        """
        This creates a map of supertype-subtype relationships.

        This is used to generate `seealso` documentation, because the
        specification type hierarchy is not always present in the generated
        code.
        """
        related_types = defaultdict(set)
        for data_type in ns.data_types:
            if not is_struct_type(data_type):
                continue

            struct_name = data_type.name

            if data_type.parent_type:
                related_types[data_type.parent_type.name].add(struct_name)
                related_types[struct_name].add(data_type.parent_type.name)

            for field in data_type.all_fields:
                if not is_struct_type(field.data_type):
                    continue

                related_types[field.data_type.name].add(struct_name)

        self._related_types = related_types

    def _generate_namespace(self, ns):
        """
        Perform code generation for the namespace.

        This calls methods that generate classes for each data type and a class
        for all the routes.

        Args:
            ns (babelapi.api.ApiNamespace): The namespace to generate.
        """
        ns_name = self._public_name(ns.name)

        self._ns = ns_name
        for data_type in ns.data_types:
            self._generate_data_type(ns_name, data_type)
        if ns.routes:
            self._generate_routes(ns, ns.routes)

    def _generate_data_type(self, ns_name, data_type):
        """
        Generate the classes for a data type.

        This generates the framework of the code file and calls an appropriate 
        method for structs and unions to generate the type itself.

        Args:
            ns_name (str): The name of the namespace.
            data_type (babelapi.data_type.DataType): The type to generate.
        """
        assert is_composite_type(data_type)
        class_name = self._public_name(data_type.name)
        with self.output_to_relative_path(os.path.join(ns_name, class_name + ".cs")):
            # this stops stylecop from analyzing the file
            self.auto_generated()

            with self.namespace(ns_name):
                # place using statements inside the namespace to make aliasing bugs
                # explicit
                self.emit('using sys = System;')
                self.emit('using col = System.Collections.Generic;')
                self.emit('using re = System.Text.RegularExpressions;')
                self.emit()
                self.emit('using enc = Dropbox.Api.Babel;')
                self.emit()

                if is_struct_type(data_type):
                    self._generate_struct(data_type)
                elif is_union_type(data_type):
                    self._generate_union(data_type)
                else:
                    assert False, 'Unknown composite type: %r' % data_type

    def _emit_encode_doc_comment(self):
        """
        Generates a standard doc comment for an encode method.
        """
        with self.doc_comment():
            self.emit_summary('Encodes the object using the supplied encoder.')
            self.emit_xml('The encoder being used to serialize the object.', 'param',
                    name='encoder')

    def _emit_decode_doc_comment(self):
        """
        Generates a standard doc comment for a decode method.
        """
        with self.doc_comment():
            self.emit_summary('Decodes on object using the supplied decoder.')
            self.emit_xml('The decoder used to deserialize the object.', 'param', name='decoder')
            self.emit_xml('The deserialized object. Note: this is not necessarily the current '
                    'instance.', 'returns')

    def _emit_explicit_interface_suppress(self):
        """
        Generates a suppression attribute that prevents a useless CodeAnalysis warning

        The warning isn't useless in general, but just isn't relevant to our use case.
        """
        self.emit('[System.Diagnostics.CodeAnalysis.SuppressMessage('
                  '"Microsoft.Design", '
                  '"CA1033:InterfaceMethodsShouldBeCallableByChildTypes")]')

    def _emit_encoder(self, field):
        """
        Emits an encoder fragment for a struct field.

        Args:
            field (babelapi.data_type.Field): The field to generate the encoder for
        """
        field_public_name = self._public_name(field.name)
        if is_nullable_type(field.data_type):
            nullable = True
            data_type = field.data_type.data_type
            if is_list_type(data_type):
                null_block = self.if_('this.{0}.Count > 0'.format(field_public_name))
            else:
                null_block = self.if_('this.{0} != null'.format(field_public_name))
            null_block.__enter__()
        else:
            nullable = False
            data_type = field.data_type
            null_block = None

        try:
            field_type = self._typename(data_type)
            if is_composite_type(data_type):
                self.emit('obj.AddFieldObject<{0}>("{1}", this.{2});'.format(
                    field_type, field.name, field_public_name))
            elif is_list_type(data_type):
                element_type = self._typename(data_type.data_type)
                if is_composite_type(data_type.data_type):
                    self.emit('obj.AddFieldObjectList<{0}>("{1}", this.{2});'.format(
                        element_type, field.name, field_public_name))
                else:
                    self.emit('obj.AddFieldList<{0}>("{1}", this.{2});'.format(
                        element_type, field.name, field_public_name))
            elif nullable and not is_string_type(field.data_type.data_type):
                self.emit('obj.AddField<{0}>("{1}", this.{2}.Value);'.format(
                    field_type, field.name, self._public_name(field.name)))
            else:
                self.emit('obj.AddField<{0}>("{1}", this.{2});'.format(
                    field_type, field.name, self._public_name(field.name)))

        finally:
            if null_block:
                null_block.__exit__(None, None, None)

    def _get_decoder_method(self, field):
        """
        Computes the decoder method that will be needed to decode the supplied
        field.

        Args:
            field (babelapi.data_type.Field): the field to be decoded.
        """
        data_type = field.data_type.data_type if is_nullable_type(field.data_type) else field.data_type 
        generic_type = self._typename(data_type)

        if is_composite_type(data_type):
            method = 'GetFieldObject'
        elif is_list_type(data_type):
            generic_type = self._typename(data_type.data_type)
            if is_composite_type(data_type.data_type):
                method = 'GetFieldObjectList'
            else:
                method = 'GetFieldList'
        else:
            method = 'GetField'

        if is_list_type(data_type):
            return 'new col.List<{0}>(obj.{1}<{0}>("{2}"))'.format(generic_type,
                                                                   method,
                                                                   field.name)
        else:
            return 'obj.{0}<{1}>("{2}")'.format(method, generic_type, field.name)

    def _emit_decoder(self, field):
        """
        Emits a decoder fragment for a struct field

        Args:
            field (babelapi.data_type.Field): The field to be decoded
        """
        field_public_name = self._public_name(field.name)

        nullable = is_nullable_type(field.data_type)
        if nullable or field.has_default:
            data_type = field.data_type.data_type if nullable else field.data_type
            null_block = self.if_('obj.HasField("{0}")'.format(field.name))
            null_block.__enter__()
        else:
            nullable = False
            data_type = field.data_type
            null_block = None

        try:
            method = self._get_decoder_method(field)
            self.emit('this.{0} = {1};'.format(field_public_name, method))
        finally:
            if null_block:
                null_block.__exit__(None, None, None)
                if is_list_type(data_type):
                    with self.else_():
                        self.emit('this.{0} = new col.List<{1}>();'.format(
                            field_public_name, self._typename(data_type.data_type)))

    def _make_struct_constructor_args(self, struct):
        """
        Creates a list of ConstructorArg instances for the fields of the 
        supplied struct. This prevents re-calculating the same information
        for each field in multiple places.

        Each entry in the returned list has the following elements
             - The C# type of the field
             - The name of the field suitable for use as an argument parameter
             - The argument declaration of the field for the constructor, this
                will include a default value where appropriate.
             - The doc string for the field.

        Args:
            struct (babelapi.data_type.Struct): The struct whose constructor 
                arguments are being enumerated.
        """
        constructor_args = []
        for field in struct.all_fields:
            fieldtype = self._typename(field.data_type)
            arg_name = self._arg_name(field.name)
            doc_name = arg_name[1:] if arg_name.startswith('@') else arg_name

            if field.has_default:
                if is_composite_type(field.data_type):
                    # we'll populate the real default when we check constraints
                    arg = '{0} {1} = null'.format(fieldtype, arg_name)
                else:
                    arg = '{0} {1} = {2}'.format(fieldtype, arg_name, self._process_literal(field.default))
            elif is_nullable_type(field.data_type):
                arg = '{0} {1} = null'.format(fieldtype, arg_name)
            else:
                arg = '{0} {1}'.format(fieldtype, arg_name)

            doc = field.doc or 'The {0}'.format(self._name_words(field.name))
            self._tag_context = (struct, True)
            doc = self.process_doc(doc, self._tag_handler)
            self._tag_context = None
            doc = '<param name="{0}">{1}</param>'.format(doc_name, doc)

            constructor_args.append(ConstructorArg(fieldtype, arg_name, arg, doc))

        return constructor_args

    def _generate_struct_init_ctor(self, struct, class_name, parent_type, parent_type_fields):
        """
        Generates the initialization constructor for a struct.

        This constructor has arguments for all fields on the struct, and
        performs validation and default handling for fields.

        Args:
            struct (babelapi.data_type.Struct): The struct for which we are
                generating a constructor.
            class_name (str): The C# class name of the struct.
            parent_type (babelapi.data_type.Struct): The parent type of this
                struct, if any.
            parent_type_fields (set): A set containing the names of fields
                that are implemented by this struct's parent type hierarchy.
        """
        ctor_args = self._make_struct_constructor_args(struct)
        super_args = []
        if parent_type:
            super_args = self._make_struct_constructor_args(parent_type)

        with self.doc_comment(data_type=struct, is_constructor=True):
            self.emit_ctor_summary(class_name)
            for arg in ctor_args:
                self.emit_wrapped_text(arg.doc)

        ctor_access = 'protected' if struct.has_enumerated_subtypes() and ctor_args else 'public'
        self.generate_multiline_list(
            [item.arg for item in ctor_args],
            before='{0} {1}'.format(ctor_access, class_name),
            skip_last_sep=True
        )
        if super_args:
            with self.indent():
                self.emit(': base({0})'.format(', '.join([item.name for item in super_args])))

        with self.cs_block():
            for field in struct.all_fields:
                # Initialize fields and check that they meet their
                # constraints according to the specification.
                if field.name in parent_type_fields:
                    continue

                has_null_check = False
                if field.has_default and is_composite_type(field.data_type):
                    self._process_composite_default(field)
                    has_null_check = True
                self._check_constraints(self._arg_name(field.name), field.data_type, has_null_check)

            for field in struct.all_fields:
                if field.name in parent_type_fields:
                    continue

                field_public_name = self._public_name(field.name)
                field_arg_name = self._arg_name(field.name)
                if (is_list_type(field.data_type) or
                    (is_nullable_type(field.data_type) and is_list_type(field.data_type.data_type))):
                    self.emit('this.{0} = {1}List;'.format(field_public_name, field_arg_name))
                else:
                    self.emit('this.{0} = {1};'.format(field_public_name, field_arg_name))


    def _generate_struct_default_ctor(self, struct, class_name, parent_type_fields):
        """
        Generates the default constructor for a struct.

        This is only relevant if the struct has fields - otherwise the
        initializing constructor is also the default constructor.

        This intializes fields to their default values if any, so that when the
        struct is being decoded, if the fields are not present in the message
        they will have their default values.

        Args:
            struct (babelapi.data_type.Struct): The struct to generate a
                constructor for.
            class_name (str): The C# class name for the struct.
            parent_type_fields (set): A set containing the names of fields
                that are implemented by this struct's parent type hierarchy.
        """
        assert len(struct.all_fields), ('Only generate a default ctor when '
                'the struct {0} has fields'.format(struct.name))

        self.emit()
        with self.doc_comment():
            self.emit_ctor_summary(class_name)
            self.emit_xml('This is to construct an instance of the object when '
                    'deserializing.', 'remarks')
        with self.cs_block(before='public {0}()'.format(class_name)):
            # initialize fields to their default values, where necessary
            for field in struct.all_fields:
                if field.name in parent_type_fields:
                    continue
                if field.has_default:
                    if is_composite_type(field.data_type):
                        self._process_composite_default(field, include_null_check=False)
                    else:
                        self.emit('this.{0} = {1};'.format(
                            self._public_name(field.name), self._process_literal(field.default)))

    def _generate_struct_strunion_is_as(self, struct):
        """
        Generates the IsFoo AsFoo properties for the subtypes of this struct.

        This is only relevant if the struct has enumerated subtypes - it is a
        strunion.

        Args:
            struct (babelapi.data_type.Struct): The struct in question.
        """
        assert struct.has_enumerated_subtypes(), ('Only generate is/as '
                'properties when the struct {0} has enumerated '
                'subtypes'.format(struct.name))

        for subtype in struct.get_enumerated_subtypes():
            subtype_type = self._typename(subtype.data_type)
            subtype_name = self._public_name(subtype.name)
            self.emit()
            with self.doc_comment():
                self.emit_summary('Gets a value indicating whether this instance is {0}'.format(subtype_name))
            with self.cs_block(before='public bool Is{0}'.format(subtype_name)):
                with self.cs_block(before='get'):
                    self.emit('return this is {0};'.format(subtype_type))

            self.emit()
            with self.doc_comment():
                self.emit_summary('Gets this instance as a <see cref="{0}" />, or <c>null</c>.'.format(subtype_type))
            with self.cs_block(before='public {0} As{1}'.format(subtype_type, subtype_name)):
                with self.cs_block(before='get'):
                    self.emit('return this as {0};'.format(subtype_type))

    def _generate_struct_properties(self, struct, parent_type_fields):
        """
        Generates the properties for struct fields.

        Args:
            struct (babelapi.data_type.Struct): The struct in question.
            parent_type_fields (set): A set containing the names of fields
                that are implemented by this struct's parent type hierarchy.
        """
        for field in struct.all_fields:
            if field.name in parent_type_fields:
                continue
            self.emit()
            doc = field.doc or 'Gets the {0} of the {1}'.format(
                self._name_words(field.name), self._name_words(struct.name))
            with self.doc_comment(data_type=struct):
                self.emit_summary(doc)

            fieldtype = self._typename(field.data_type, is_property=True)
            setter_access = 'protected' if struct.has_enumerated_subtypes() else 'private'
            self.emit('public {0} {1} {{ get; {2} set; }}'.format(fieldtype,
                                                                  self._public_name(field.name),
                                                                  setter_access))

    def _get_struct_tag(self, struct):
        if struct.parent_type and struct.parent_type.has_enumerated_subtypes():
            for subtype in struct.parent_type.get_enumerated_subtypes():
                if subtype.data_type is struct:
                    return subtype.name

    def _generate_struct_encodable_methods(self, struct, class_name, parent_type_fields):
        """
        Generates the Encode and Decode methods that make this class implement
        the IEncodable<T> interface.

        Args:
            struct (babelapi.data_type.Struct): The struct in question.
            class_name (str): The C# class name for the struct
            parent_type_fields (set): A set containing the names of fields
                that are implemented by this struct's parent type hierarchy.
        """
        self.emit()
        with self.region('IEncodable<{0}> methods'.format(class_name)):
            # Emit the encoder
            self._emit_encode_doc_comment()
            self._emit_explicit_interface_suppress()
            with self.cs_block(before='void enc.IEncodable<{0}>.Encode(enc.IEncoder encoder)'.format(class_name)):
                if struct.has_enumerated_subtypes():
                    for index, subtype in enumerate(struct.get_enumerated_subtypes()):
                        cond = self.if_ if not index else self.else_if
                        subtype_public_name = self._public_name(subtype.name)
                        subtype_typename = self._typename(subtype.data_type)
                        with cond('this.Is{0}'.format(subtype_public_name)):
                            self.emit('((enc.IEncodable<{0}>)this.As{1}).Encode(encoder);'.format(
                                subtype_typename, subtype_public_name))

                    with self.else_():
                        if not struct.is_catch_all():
                                self.emit('throw new sys.InvalidOperationException();')
                        else:
                            with self.using('var obj = encoder.AddObject()'):
                                tag = self._get_struct_tag(struct)
                                self.emit('obj.AddField<string>(".tag", "{0}");'.format(tag or ''))
                                for field in struct.all_fields:
                                    self._emit_encoder(field)
                elif struct.parent_type and struct.parent_type.has_enumerated_subtypes():
                    tag = self._get_struct_tag(struct);
                    assert tag is not None, 'Tag should not be none within a subtype hierarchy'

                    with self.using('var obj = encoder.AddObject()'):
                        self.emit('obj.AddField<string>(".tag", "{0}");'.format(tag))
                        for field in struct.all_fields:
                            self._emit_encoder(field)
                else:
                    with self.using('var obj = encoder.AddObject()'):
                        for field in struct.all_fields:
                            if field.name in parent_type_fields:
                                continue
                            self._emit_encoder(field)

            # Emit the decoder
            self.emit()
            self._emit_decode_doc_comment()
            self._emit_explicit_interface_suppress()
            with self.cs_block(before='{0} enc.IEncodable<{0}>.Decode(enc.IDecoder decoder)'.format(class_name)):
                if struct.has_enumerated_subtypes():
                    self.emit('var tag = string.Empty;')
                    with self.using('var obj = decoder.GetObject()'):
                        self.emit('tag = obj.GetField<string>(".tag");')
                    self.emit()
                    with self.switch('tag'):
                        for subtype in struct.get_enumerated_subtypes():
                            with self.case('"{0}"'.format(subtype.name), needs_break=False):
                                subtype_arg_name = self._arg_name(subtype.name)
                                subtype_typename = self._typename(subtype.data_type)
                                self.emit('var {0} = new {1}();'.format(subtype_arg_name, subtype_typename))
                                self.emit('return ((enc.IEncodable<{0}>){1}).Decode(decoder);'.format(
                                    subtype_typename, subtype_arg_name))
                        if struct.is_catch_all():
                            with self.case(needs_break=False):
                                with self.using('var obj = decoder.GetObject()'):
                                    for field in struct.all_fields:
                                        self._emit_decoder(field) 
                                self.emit()
                                self.emit('return this;')
                        else:
                            with self.case(needs_break=False):
                                self.emit('throw new sys.InvalidOperationException();')
                else:
                    with self.using('var obj = decoder.GetObject()'):
                        for field in struct.all_fields:
                            self._emit_decoder(field) 
                    self.emit()
                    self.emit('return this;')

    def _generate_struct(self, struct):
        """
        Generates the class for a struct.

        This performs the following steps.
            - Emits class documentation
            - Emits the class declaration
            - Emits a constructor that takes arguments to initialize the fields
            - If there are fields, emits a default constructor which will be
                used by the deserialization process, and which initializes 
                fields to their default values.
            - If this struct has enumerated subtypes, then emit accessor
                properties for those subtypes
            - Emits properties for fields (not including fields in parent 
                types)
            - Emits the encoder and decoder implementations
        """
        with self.doc_comment(data_type=struct):
            self.emit_summary(struct.doc or 'The {0} object'.format(self._name_words(struct.name)))
            for related in sorted(self._related_types[struct.name]):
                self.emit_xml(None, 'seealso', cref=self._public_name(related))

        class_name = self._public_name(struct.name)

        if struct.parent_type and struct.parent_type.has_enumerated_subtypes():
            parent_type = struct.parent_type
            inherits = [self._public_name(parent_type.name)]
            parent_type_fields = set(f.name for f in parent_type.all_fields)
        else:
            parent_type = None
            parent_type_fields = set()
            inherits = []

        inherits.append('enc.IEncodable<{0}>'.format(class_name))
        access = 'public' if struct.has_enumerated_subtypes() else 'public sealed'
        with self.class_(class_name, inherits=inherits, access=access):

            # Generate the initializing constructor.
            self._generate_struct_init_ctor(struct, class_name, parent_type, parent_type_fields)

            # Generate a default constructor
            if len(struct.all_fields):
                # the default constructor is only needed if the struct has fields
                self._generate_struct_default_ctor(struct, class_name, parent_type_fields)

            if struct.has_enumerated_subtypes():
                # Generate properties for checking/getting the actual type
                self._generate_struct_strunion_is_as(struct)

            # Emit properties for all fields
            self._generate_struct_properties(struct, parent_type_fields)

            # Emit methods that make this object IEncodable
            self._generate_struct_encodable_methods(struct, class_name, parent_type_fields)

    def _generate_union_is_as_properties(self, union):
        """
        Generates this IsFoo AsFoo properties for the union fields.

        These properties allow code to check and cast a union instances.

        Args:
            union (babelapi.data_type.Union): The union in question.
        """
        for field in union.fields:
            field_type = self._public_name(field.name)
            self.emit();
            with self.doc_comment():
                self.emit_summary('Gets a value indicating whether this instance is {0}'.format(field_type))
            with self.cs_block(before='public bool Is{0}'.format(field_type)):
                with self.cs_block(before='get'):
                    self.emit('return this is {0};'.format(field_type))

            self.emit();
            with self.doc_comment():
                self.emit_summary('Gets this instance as a {0}, or <c>null</c>.'.format(field_type))
            with self.cs_block(before='public {0} As{0}'.format(field_type)):
                with self.cs_block(before='get'):
                    self.emit('return this as {0};'.format(field_type))

    def _generate_union_encodable_methods(self, union, class_name):
        """
        Generates the IEncodable<T> methods for a union.

        Args:
            union (babelapi.data_type.Union): The union in question.
            class_name (str): The C# class name of the union.
        """
        self.emit()
        with self.region('IEncodable<{0}> methods'.format(class_name)):
            # encoder - is the responsibility of the concrete classes
            self._emit_encode_doc_comment()
            self._emit_explicit_interface_suppress()
            with self.cs_block(before='void enc.IEncodable<{0}>.Encode(enc.IEncoder encoder)'.format(class_name)):
                has_catch_all = False
                for index, field in enumerate(union.fields):
                    public_field_name = self._public_name(field.name)
                    condition = 'this.Is{0}'.format(public_field_name)
                    if index == 0:
                        cond = self.if_(condition)
                    elif union.catch_all_field == field:
                        cond = self.else_()
                        has_catch_all = True
                    else:
                        cond = self.else_if(condition)

                    with cond:
                        self.emit('((enc.IEncodable<{0}>)this).Encode(encoder);'.format(public_field_name))

                if not has_catch_all:
                    with self.else_():
                        self.emit('throw new sys.InvalidOperationException();')

            # decoder
            self.emit()
            self._emit_decode_doc_comment()
            self._emit_explicit_interface_suppress()
            with self.cs_block(before='{0} enc.IEncodable<{0}>.Decode(enc.IDecoder decoder)'.format(class_name)):
                with self.switch('decoder.GetUnionName()'):
                    for field in union.fields:
                        constant = None if union.catch_all_field == field else '"{0}"'.format(field.name)
                        with self.case(constant, needs_break=False):
                            public_field_name = self._public_name(field.name)
                            if is_void_type(field.data_type):
                                self.emit('return {0}.Instance;'.format(public_field_name))
                            elif is_composite_type(field.data_type):
                                arg_name = self._arg_name(field.name)
                                type_name = self._typename(field.data_type)
                                self.emit('var {0} = new {1}();'.format(arg_name, type_name))                          
                                self.emit('return new {0}(((enc.IEncodable<{1}>){2}).Decode(decoder));'.format(
                                    public_field_name, type_name, arg_name))
                            else:
                                with self.using('var obj = decoder.GetObject()'):
                                    method = self._get_decoder_method(field)
                                    self.emit('return new {0}({1});'.format(public_field_name, method))

                    if not union.catch_all_field:
                        self.emit('default:')
                        with self.indent():
                            self.emit('throw new sys.InvalidOperationException();')

    def _generate_union_field_void_type(self, field, field_type):
        """
        Generates the inner type for a union field that is void.

        This has a private constructor and a singleton static instance.

        Args:
            field (babelapi.data_type.UnionField): The union field in question.
            field_type (str): The C# type name of the union field.
        """
        # constructor
        with self.doc_comment():
            self.emit_ctor_summary(field_type)
        with self.cs_block(before='private {0}()'.format(field_type)):
            pass

        # singleton instance
        self.emit()
        with self.doc_comment():
            self.emit_summary('A singleton instance of {0}'.format(field_type))
        self.emit('public static readonly {0} Instance = new {0}();'.format(field_type))

        # encoder
        self.emit()
        self._emit_encode_doc_comment()
        self._emit_explicit_interface_suppress()
        with self.cs_block(before='void enc.IEncodable<{0}>.Encode(enc.IEncoder encoder)'.format(field_type)):
            with self.using('var obj = encoder.AddObject()'):
                self.emit('obj.AddField(".tag", "{0}");'.format(field.name))

        self._generate_union_field_decoder(field_type)

    def _generate_union_field_value_type(self, field, field_type):
        """
        Generates the inner type for a union field that has a value.

        This has a public constructor and a Value property.

        Args:
            field (babelapi.data_type.UnionField): The union field in question.
            field_type (str): The C# type name of the union field.
        """
        with self.doc_comment():
            self.emit_ctor_summary(field_type)
            self.emit('<param name="value">The value</param>')

        value_type = self._typename(field.data_type)
        with self.cs_block(
                before='public {0}({1} value)'.format(field_type, value_type)):
            if is_list_type(field.data_type):
                self.emit('this.Value = new col.List<{0}>(value);'.format(
                        self._typename(field.data_type.data_type)))
            else:
                self.emit('this.Value = value;')
        self.emit()
        with self.doc_comment():
            self.emit_summary('Gets the value of this instance.')
        value_type = self._typename(field.data_type, is_property=True)
        self.emit('public {0} Value {{ get; private set; }}'.format(value_type))

        # encoder
        self.emit()
        self._emit_encode_doc_comment()
        with self.cs_block(before='void enc.IEncodable<{0}>.Encode(enc.IEncoder encoder)'.format(field_type)):
            with self.using('var obj = encoder.AddObject()'):
                self.emit('obj.AddField(".tag", "{0}");'.format(field.name))
                self.emit('obj.AddField("{0}", this.Value);'.format(field.name))

        self._generate_union_field_decoder(field_type)

    def _generate_union_field_decoder(self, field_type):
        """
        Emits the decoder for union field inner type.

        This decoder just throws a exception. Decoding always happens through
        the union class.

        Args:
            field_type (str): The C# type name of the union field.
        """
        # decoder
        self.emit()
        self._emit_decode_doc_comment()
        self._emit_explicit_interface_suppress()
        with self.cs_block(before='{0} enc.IEncodable<{0}>.Decode(enc.IDecoder decoder)'.format(field_type)):
            self.emit('throw new sys.InvalidOperationException("Decoding happens through the base class");')


    def _generate_union_field_type(self, field, class_name):
        """
        Generates the inner class for a union field.

        Args:
            field (babelapi.data_type.UnionField): The union field in question.
            class_name (str): The C# type name of the parent union.
        """
        field_type = self._public_name(field.name)
        self.emit()

        with self.doc_comment():
            self.emit_summary(field.doc or 'The {0} object'.format(self._name_words(field.name)))
        inherits = (class_name, 'enc.IEncodable<{0}>'.format(field_type))
        with self.class_(field_type, inherits=inherits, access='public sealed'):
            if is_void_type(field.data_type):
                self._generate_union_field_void_type(field, field_type)
            else:
                self._generate_union_field_value_type(field, field_type)

    def _generate_union(self, union):
        """
        Generates the class for a union.

        This performs the following steps
            - Creates a name context with the union field names, this protects
                against name collisions when resolving names.
            - Generates the class level documentation for the union class
            - Generates the class and its default constructor
            - Generates type helper ('Is<field>' and 'As<field>') properties
            - Generates encodable methos
            - Generates an inner type for each union field.

        Args:
            union (babelapi.data_type.Union): The union in question.
        """
        union_field_names = [self._public_name(f.name) for f in union.fields]
        with self._local_names(union_field_names):
            with self.doc_comment():
                self.emit_summary(union.doc or 'The {0} object'.format(self._name_words(union.name)))
            class_name = self._public_name(union.name)
            with self.class_(class_name, inherits='enc.IEncodable<{0}>'.format(class_name), access='public'):
                with self.doc_comment():
                    self.emit_ctor_summary(class_name)
                with self.cs_block(before='public {0}()'.format(class_name)):
                    pass

                # generate type helper properties
                self._generate_union_is_as_properties(union)

                # generate encodable
                self._generate_union_encodable_methods(union, class_name)

                # generate j types for each union field
                for field in union.fields:
                    self._generate_union_field_type(field, class_name)

    def _generate_routes(self, ns, routes):
        """
        Generates the class that encapsulates the routes in this namespace.

        This class has methods for each route and is constructed with an
        instance of the ITransport interface.

        Args:
            ns (babelapi.api.ApiNamespace): The namespace.
            routes (iterable of babelapi.api.ApiRoute): The routes in this
                namespace.
        """
        ns_name = self._public_name(ns.name)
        class_name = ns_name + 'Routes'
        with self.output_to_relative_path(os.path.join(ns_name, class_name +  '.cs')):
            # this stops stylecop from analyzing the file
            self.auto_generated()
            with self.namespace('.'.join([ns_name, 'Routes'])):
                self.emit('using sys = System;')
                self.emit('using io = System.IO;')
                self.emit('using col = System.Collections.Generic;')
                self.emit('using t = System.Threading.Tasks;')
                self.emit('using enc = Dropbox.Api.Babel;')
                self.emit()

                with self.doc_comment():
                    self.emit_summary('The routes for the <see cref="N:{0}{1}"/> namespace'.format(
                        self.DEFAULT_NAMESPACE, ns_name))
                with self.class_(class_name, access='public'):
                    with self.doc_comment():
                        self.emit_ctor_summary(class_name)
                        self.emit_xml('The transport to use', 'param', name='transport')
                    with self.cs_block(before='internal {0}(enc.ITransport transport)'.format(
                            class_name)):
                        self.emit('this.Transport = transport;')

                    self.emit()
                    with self.doc_comment():
                        self.emit_summary('Gets the transport used for these routes')
                    self.emit('internal enc.ITransport Transport { get; private set; }')

                    for route in routes:
                        self._generate_route(ns, route)

    def _generate_route(self, ns, route):
        """
        Generates the methods that allow a route to be called.

        The route has at least one, maybe two, *Async() methods - there is only
        one method if the request type is void or has no fields, otherwise there
        are two, one with the request type explicitly and another with the 
        request type constructor arguments.

        For each *Async method there is a Begin* method with the same arguments - 
        plus callback and state arguments.

        There is one End* method generated.

        Args:
            ns (babelapi.api.ApiNamespace): The namespace of the route.
            route (babelapi.api.ApiRoute): The route in question.
        """
        public_name = self._public_name(route.name)
        member_name = '{0}Async'.format(public_name)

        route_host = route.attrs.get('host', 'api')
        route_style = route.attrs.get('style', 'rpc')

        request_type = self._typename(route.request_data_type, void='enc.Empty')
        request_is_void = is_void_type(route.request_data_type)
        request_arg = (self._arg_name(route.request_data_type.name) if
                is_composite_type(route.request_data_type) else 'request')
        response_type = self._typename(route.response_data_type, void='enc.Empty')
        response_is_void = is_void_type(route.response_data_type)
        error_type = self._typename(route.error_data_type, void='enc.Empty')
        error_is_void = is_void_type(route.error_data_type)

        if response_is_void:
            task_type = 't.Task'
            apm_response_type = 'void'
        elif route_style == 'download':
            task_type = 't.Task<enc.IDownloadResponse<{0}>>'.format(response_type)
            apm_response_type = 'enc.IDownloadResponse<{0}>'.format(response_type)
        else:
            task_type = 't.Task<{0}>'.format(response_type)
            apm_response_type = response_type

        ctor_args = []
        route_args = []
        if not request_is_void:
            route_args.append("{0} {1}".format(request_type, request_arg))
            if is_struct_type(route.request_data_type):
                ctor_args = self._make_struct_constructor_args(route.request_data_type)
        if route_style == 'upload':
            route_args.append("io.Stream body")
            if next((c.arg for c in ctor_args if '=' in c.arg), False):
                body_arg = 'io.Stream body = null'
            else:
                body_arg = 'io.Stream body'
            ctor_args.append(ConstructorArg('io.Stream', 'body', body_arg, '<param name="body">The document to upload</param>'))
       
        async_fn = 'public {0} {1}({2})'.format(task_type, member_name, ', '.join(route_args)) 

        apm_args = route_args + ['sys.AsyncCallback callback', 'object state = null']
        apm_fn = 'public sys.IAsyncResult Begin{0}({1})'.format(public_name, ', '.join(apm_args))

        type_args = (request_type, response_type, error_type)

        self.emit()
        with self.doc_comment():
            self.emit_summary(route.doc or 'The {0} route'.format(self._name_words(route.name)))
            if not request_is_void:
                self.emit_xml('The request parameters', 'param', name=request_arg)
            if route_style == 'upload':
                self.emit_xml('The content to upload.', 'param', name='body')
            if response_is_void:
                self.emit_xml('The task that represents the asynchronous send operation.',
                        'returns')
            else:
                self.emit_xml('The task that represents the asynchronous send operation. '
                        'The TResult parameter contains the response from the server.',
                        'returns')
            if not error_is_void:
                self.emit_xml('Thrown if there is an error processing the request; '
                              'This will contain a <see cref="{0}"/>.'.format(error_type),
                              'exception', cref='Dropbox.Api.ApiException{{{0}}}'.format(error_type))

        with self.cs_block(before=async_fn):
            args = ['enc.Empty.Instance' if request_is_void else request_arg]
            if route_style == 'upload':
                args.append('body')
            args.extend([
                '"{0}"'.format(route_host),
                '"/{0}/{1}"'.format(ns.name, route.name)
            ])

            self.emit('return this.Transport.Send{0}RequestAsync<{1}>({2});'.format(
                self._public_name(route_style),
                ', '.join(type_args),
                ', '.join(args)))

        self.emit()
        with self.doc_comment():
            self.emit_summary('Begins an asynchronous send to the {0} route.'.format(self._name_words(route.name)))
            if not request_is_void:
                self.emit_xml('The request parameters.', 'param', name=request_arg)
            if route_style == 'upload':
                self.emit_xml('The content to upload.', 'param', name='body')
            self.emit_xml('The method to be called when the asynchronous send is completed.',
                    'param', name='callback')
            self.emit_xml('A user provided object that distinguished this send from other send '
                    'requests.', 'param', name='state')
            self.emit_xml('An object that represents the asynchronous send request.', 'returns')
        with self.cs_block(before=apm_fn):
            async_args = []
            if not request_is_void:
                async_args.append(request_arg)
            if route_style == 'upload':
                async_args.append('body')

            self.emit('var task = this.{0}({1});'.format(member_name, ', '.join(async_args)))
            self.emit()
            self.emit('return enc.Util.ToApm(task, callback, state);')

        if len(ctor_args) > (1 if route_style == 'upload' else 0):
            arg_list = [item.arg for item in ctor_args]
            arg_name_list = [item.name for item in ctor_args]

            self.emit()
            with self.doc_comment():
                self.emit_summary(route.doc or 'The {0} route'.format(self._name_words(route.name)))
                for arg in ctor_args:
                    self.emit_wrapped_text(arg.doc)
                if response_is_void:
                    self.emit_xml('The task that represents the asynchronous send operation.',
                            'returns')
                else:
                    self.emit_xml('The task that represents the asynchronous send operation. '
                            'The TResult parameter contains the response from the server.',
                            'returns')
                if not error_is_void:
                    self.emit_xml('Thrown if there is an error processing the request; '
                                  'This will contain a <see cref="{0}"/>.'.format(error_type),
                                  'exception', cref='Dropbox.Api.ApiException{{{0}}}'.format(error_type))
            self.generate_multiline_list(
                arg_list,
                before='public {0} {1}'.format(task_type, member_name),
                skip_last_sep=True
            )
            with self.cs_block():
                self.generate_multiline_list(
                    arg_name_list[:-1] if route_style == 'upload' else arg_name_list,
                    before='var {0} = new {1}'.format(request_arg, request_type),
                    after=';',
                    skip_last_sep=True
                )
                self.emit()
                async_args = [request_arg]
                if route_style == 'upload':
                    async_args.append('body')
                self.emit('return this.{0}({1});'.format(member_name, ', '.join(async_args)))

            self.emit()
            with self.doc_comment():
                self.emit_summary('Begins an asynchronous send to the {0} route.'.format(
                        self._name_words(route.name)))
                for arg in ctor_args:
                    self.emit_wrapped_text(arg.doc)
                self.emit_xml('The method to be called when the asynchronous send is completed.',
                    'param', name='callback')
                self.emit_xml('A user provided object that distinguished this send from other '
                    'send requests.', 'param', name='callbackState')
                self.emit_xml('An object that represents the asynchronous send request.',
                        'returns')

            if next((arg for arg in arg_list if '=' in arg), False):
                arg_list.append('sys.AsyncCallback callback = null')
            else:
                arg_list.append('sys.AsyncCallback callback')
            arg_list.append('object callbackState = null')

            self.generate_multiline_list(
                    arg_list,
                    before='public sys.IAsyncResult Begin{0}'.format(public_name),
                    skip_last_sep=True)
            with self.cs_block():
                self.generate_multiline_list(
                    arg_name_list[:-1] if route_style == 'upload' else arg_name_list,
                    before='var {0} = new {1}'.format(request_arg, request_type),
                    after=';',
                    skip_last_sep=True
                )
                self.emit()
                args = [request_arg]
                if route_style == 'upload':
                    args.append('body')
                args.extend(['callback', 'callbackState'])
                self.emit('return this.Begin{0}({1});'.format(public_name, ', '.join(args)))

        self.emit()
        with self.doc_comment():
            self.emit_summary('Waits for the pending asynchronous send to the {0} route to complete'.format(
                    self._name_words(route.name)))
            self.emit_xml('The reference to the pending asynchronous send request', 'param',
                    name='asyncResult')
            if not response_is_void:
                self.emit_xml('The response to the send request', 'returns')
            if not error_is_void:
                self.emit_xml('Thrown if there is an error processing the request; '
                              'This will contain a <see cref="{0}"/>.'.format(error_type),
                              'exception', cref='Dropbox.Api.ApiException{{{0}}}'.format(error_type))
        with self.cs_block(before='public {0} End{1}(sys.IAsyncResult asyncResult)'.format(
                apm_response_type, public_name)):
            self.emit('var task = asyncResult as {0};'.format(task_type))
            with self.if_('task == null'):
                self.emit('throw new sys.InvalidOperationException();')
            if not response_is_void:
                self.emit()
                self.emit('return task.Result;')