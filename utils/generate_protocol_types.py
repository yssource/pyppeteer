"""
CLI script to generate Protocol types
"""
import asyncio
import json
import logging
import re
import time
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from pathlib import Path
from textwrap import dedent
from typing import Dict, Any, Hashable, Match, List, Union

import networkx as nx

from pyppeteer import launch

handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('[{levelname}] {name}: {message}', style='{'))
logging.getLogger('pyppeteer').addHandler(handler)

logger = logging.getLogger('CLI')
logger.addHandler(handler)
logger.setLevel(logging.INFO)
handler.setLevel(logging.INFO)


class ProtocolTypesGenerator:
    _forward_ref_re = r'\'Protocol\.(\w+\.\w+)\''
    js_to_py_types = {
        'any': 'Any',
        'string': 'str',
        'object': 'Dict[str, str]',
        'boolean': 'bool',
        'number': 'float',
        'integer': 'int',
        'binary': 'bytes',
    }
    MAX_RECURSIVE_TYPE_EXPANSION_DEPTH = 2

    def __init__(self):
        self.domains = []
        # cache of all known types
        self.all_known_types = {}
        self.typed_dicts = {}
        # store all references from one TypedDict to another
        self.td_cross_references = {}
        self.code_gen = TypingCodeGenerator()

    @property
    def header(self) -> str:
        return f'''\
            Automatically generated by ./utils/generate_protocol_types
            Attention! This file should not be modified directly! Instead, use the script to create it. 
    
            Last regeneration: {datetime.utcnow()}'''

    def _resolve_forward_ref_re_sub_repl(self, match: Match) -> str:
        ref, domain_ = None, None
        try:
            domain_, ref = match.group(1).split('.')
            resolved_fwref = self.all_known_types[domain_][ref]

            # resolve nested forward references
            if re.search(self._forward_ref_re, resolved_fwref):
                resolved_fwref = self.resolve_forward_ref_on_line(resolved_fwref)
            if (
                resolved_fwref not in self.js_to_py_types.values()
                and not resolved_fwref.startswith('Literal')
                and not resolved_fwref.startswith('List')
            ):
                # forward ref to a typed dict, not sure that it will be defined
                resolved_fwref = f'\'{resolved_fwref}\''
        except ValueError:  # too few values to unpack, ie malformed forward reference
            raise ValueError(f'Forward reference not nested as expected (forward reference={match.group(0)})')

        return resolved_fwref

    def resolve_forward_ref_on_line(self, line: str) -> str:
        """
        Replaces a forward reference in the form 'Protocol.domain.ref' to the actual value of Protocol.domain.ref
        :param line: line in which protocol forward reference occurs.
        :return: line with resolved forward reference
        """
        return re.sub(self._forward_ref_re, self._resolve_forward_ref_re_sub_repl, line)

    async def _retrieve_top_level_domain(self):
        browser = await launch(args=['--no-sandbox', '--disable-setuid-sandbox'])
        base_endpoint = re.search(r'ws://([0-9A-Za-z:.]*)/', browser.wsEndpoint).group(1)
        page = await browser.newPage()

        logger.info(f'Loading protocol into memory')
        t_start = time.perf_counter()

        await page.goto(f'http://{base_endpoint}/json/protocol')
        page_content = await page.evaluate('document.documentElement.innerText')
        try:
            await browser.close()
        except Exception as e:
            logger.warning(f'Exception on browser close: {e}')

        logger.info(f'Loaded protocol into memory in {time.perf_counter()-t_start:.2f}s')
        self.domain = json.loads(page_content)

    def retrieve_top_level_domain(self):
        """
        Fetches as sets the class variable domains for later use.
        :return: None
        """
        asyncio.get_event_loop().run_until_complete(self._retrieve_top_level_domain())

    def gen_spec(self):
        """
        Generate the Protocol class file lines within self.code_gen attribute. Uses an IndentManager context manager to 
        keep track of the current indentation level. Resolves all forward references. Expands self-recursive types to
        MAX_RECURSIVE_TYPE_EXPANSION_DEPTH. Expands cyclic types to 1 level.
        :return: None
        """

        self.code_gen.insert_before_code('"""')
        self.code_gen.insert_before_code(self.header)
        self.code_gen.insert_before_code('"""')
        self.code_gen.add_newlines(num=2)

        logger.info(f'Generating protocol spec')
        t_start = time.perf_counter()

        self.code_gen.add('class Protocol:')
        with self.code_gen.indent_manager:
            for domain in self.domain['domains']:
                domain_name = domain['domain']
                self.code_gen.add(f'class {domain_name}:')
                self.code_gen.add_comment_from_info(domain)
                self.all_known_types[domain_name] = domain_known_types = {}
                with self.code_gen.indent_manager:
                    for type_info in domain.get('types', []):
                        self.code_gen.add_comment_from_info(type_info)
                        item_name = type_info["id"]
                        if 'properties' in type_info:
                            # name mangled to avoid collisions
                            _type = f'_{domain_name}_{type_info["id"]}'
                            self.typed_dicts.update(self.generate_typed_dicts(type_info, domain_name, name=_type))
                        else:
                            _type = self.convert_js_to_py_type(type_info, domain_name)

                        domain_known_types[item_name] = _type
                        self.code_gen.add(f'{item_name} = {_type}')

                    for command_info in domain.get('commands', []):
                        item_name = command_info["name"]
                        self.code_gen.add_comment_from_info(command_info)
                        if 'parameters' in command_info:
                            _type = f'{command_info["name"]}_{domain_name}_Parameters'
                            self.typed_dicts.update(self.generate_typed_dicts(command_info, domain_name, name=_type))
                        else:
                            _type = 'None'

                        domain_known_types[f'{item_name}Parameters'] = _type
                        self.code_gen.add(f'{item_name}Parameters = {_type}')

                        if 'returns' in command_info:
                            _type = f'{command_info["name"]}_{domain_name}_ReturnValue'
                            self.typed_dicts.update(self.generate_typed_dicts(command_info, domain_name, name=_type))
                        else:
                            _type = 'None'

                        domain_known_types[f'{item_name}ReturnValue'] = _type
                        self.code_gen.add(f'{item_name}ReturnValue = {_type}')

                    self.code_gen.add_newlines(num=1)

            self.code_gen.add(f'class Events:')
            with self.code_gen.indent_manager:
                for domain in self.domain['domains']:
                    for event in domain.get('events', []):
                        self.code_gen.add(
                            f'{domain["domain"]}.{event["name"]} = '
                            f'\'Protocol.{domain["domain"]}.{event["name"]}Payload\''
                        )

            self.code_gen.add(f'class CommandParameters:')
            with self.code_gen.indent_manager:
                for domain in self.domain['domains']:
                    for command in domain.get('commands', []):
                        self.code_gen.add(
                            f'{domain["domain"]}.{command["name"]} = '
                            f'\'Protocol.{domain["domain"]}.{command["name"]}Parameters\''
                        )

            self.code_gen.add(f'class CommandReturnValues:')
            with self.code_gen.indent_manager:
                for domain in self.domain['domains']:
                    for command in domain.get('commands', []):
                        self.code_gen.add(
                            f'{domain["domain"]}.{command["name"]} = '
                            f'\'Protocol.{domain["domain"]}.{command["name"]}ReturnValue\''
                        )

        # no need for copying list as we aren't adding/removing elements
        # resolve forward refs in main protocol class
        for index, line in enumerate(self.code_gen.code_lines):
            # skip empty lines or lines positively without forward reference
            if not line.strip() or 'Protocol' not in line:
                continue
            self.code_gen.code_lines[index] = self.resolve_forward_ref_on_line(line)

        # resolve forward refs in typed dicts, and store instances where TypedDict is referenced somewhere else
        for td_name, td in self.typed_dicts.items():
            for index, line in enumerate(td.code_lines):
                resolved_fw_ref = self.resolve_forward_ref_on_line(line)
                resolved_fw_ref_splits = resolved_fw_ref.split(': ')
                if len(resolved_fw_ref_splits) == 2:  # only pay attention to actual resolve fw refs
                    ref = resolved_fw_ref_splits[1]
                    if re.search('_\w+_\w+', ref):
                        if td_name not in self.td_cross_references:
                            self.td_cross_references[td_name] = []
                        if ref.strip("'") not in self.td_cross_references[td_name]:
                            self.td_cross_references[td_name].append(ref.strip("'"))
                self.typed_dicts[td_name].code_lines[index] = resolved_fw_ref

        edges = []
        for node, refs in self.td_cross_references.items():
            for ref in refs:
                edges.append((node, re.search(r'\'?(_\w+_\w+)\'?', ref).group(1)))

        # fix cyclic references by finding them and replacing them
        for start, cycling_start in nx.simple_cycles(nx.DiGraph(edges)):
            expanded_cyclic_reference = f'{start}_cyclic_ref{cycling_start}'
            self.typed_dicts[expanded_cyclic_reference] = self.typed_dicts[cycling_start].copy_with_filter(
                expanded_cyclic_reference, r'\'_\w+_\w+\'', 'Any'
            )
            td_1 = self.typed_dicts[start]
            for index, line in enumerate(td_1.code_lines):
                if cycling_start in line:
                    self.typed_dicts[start].code_lines[index] = line.replace(
                        f'\'{cycling_start}\'', f'\'{expanded_cyclic_reference}\''
                    )

        # all typed dicts are inserted prior to the main Protocol class
        last_item_index = len(type_info) - 1
        for index, td in enumerate(self.typed_dicts.values()):
            if index < last_item_index:
                td.add_newlines(num=1)
            self.code_gen.insert_before_code(td)

        logger.info(f'Parsed protocol spec in {time.perf_counter()-t_start:.2f}s')
        # newline at end of file
        self.code_gen.add_newlines(num=1)

    def write_generated_code(self, path: Path = Path('protocol.py')) -> None:
        """
        Write generated code lines to the specified path. Writes to a temporary file and checks that file with mypy to
        'resolve' any cyclic references.

        :param path: path to write type code to.
        :return: None
        """
        if path.is_dir():
            path /= 'protocol.py'
        logger.info(f'Writing generated protocol code to {path}')
        # noinspection PyTypeChecker
        with open(path, 'w') as p:
            p.write(str(self.code_gen))

    def generate_typed_dicts(
        self, type_info: Dict[str, Any], domain_name: str, name: str = None, _depth: int = 0
    ) -> Dict[str, 'TypedDictGenerator']:
        """
        Generates TypedDicts based on type_info. If the TypedDict references itself, the recursive type reference is
        expanded upon MAX_RECURSIVE_TYPE_EXPANSION_DEPTH times.

        :param type_info: Dict containing the info for the TypedDict
        :param domain_name: path to resolve relative forward references in type_info against
        :param name: (Optional) Name of TypedDict. Defaults to name found in type_info
        :param _depth: Internally used param to track recursive function call depth.
        :return: List of TypedDicts corresponding to type information found in type_info
        """

        items = self._multi_fallback_get(type_info, 'returns', 'parameters', 'properties')
        type_info_name = self._multi_fallback_get(type_info, 'id', 'name')
        recursive_ref = self.get_forward_ref(type_info_name, domain_name)
        td_name = name or type_info_name
        is_total = any(1 for x in items if x.get('optional'))
        base_td = TypedDictGenerator(td_name, is_total)
        tds = {td_name: base_td}
        with base_td.indent_manager:
            non_recursive_ref = None
            for item in items:
                base_td.add_comment_from_info(item)
                _type = self.convert_js_to_py_type(item, domain_name)

                if recursive_ref in _type:
                    if non_recursive_ref is None:
                        if _depth >= self.MAX_RECURSIVE_TYPE_EXPANSION_DEPTH:
                            # last ditch recursive reference expansion to expand the type 2x more
                            non_recursive_ref = 'Dict[str, Dict[str, Any]]'
                        else:
                            if 'FWRef' in td_name:
                                td_name = td_name.replace(f'FWRef{_depth-1}', f'FWRef{_depth}')
                            else:
                                td_name += f'_FWRef{_depth}'
                            tds.update(self.generate_typed_dicts(type_info, domain_name, td_name, _depth + 1))
                            non_recursive_ref = td_name
                    _type = _type.replace(recursive_ref, non_recursive_ref)

                base_td.add(f'{item["name"]}: {_type}')

        return {k: v for k, v in sorted(tds.items(), key=lambda x: x[0])}

    @staticmethod
    def _multi_fallback_get(d: Dict[Hashable, Any], *k: Hashable):
        """
        Convenience method to retrieve item from dict with multiple keys as fallbacks for failed accesses
        :param d: Dict to retrieve values from
        :param k: keys of Dict to retrieve values from
        :return: first found value where key in k
        """
        for key in k:
            if key in d:
                return d[key]

        raise KeyError(f'{", ".join([str(s) for s in k])} all not found in {d}')

    def convert_js_to_py_type(self, type_info: Union[Dict[str, Any], str], domain_name) -> str:
        """
        Generates a valid python type from the JS type. In the case of type_info being a str, we simply return the
        matching python type from self.js_to_py_types. Otherwise, in the case of type_info being a Dict, we know that
        it will contain vital information about the type we are trying to convert.

        The domain_name is used to qualify relative forward reference in type_info. For example, if
        type_info['$ref'] == 'foo', domain_name would be used produce an absolute forward reference, ie domain_name.foo

        :param type_info: Dict or str containing type_info
        :param domain_name: path to resolve relative forward references in type_info against
        :return: valid python type, either in the form of an absolute forward reference (eg Protocol.bar.foo) or
            primitive type (eg int, float, str, etc)
        """
        if isinstance(type_info, str):
            _type = self.js_to_py_types[type_info]
        elif 'items' in type_info:
            assert type_info['type'] == 'array'
            if '$ref' in type_info['items']:
                ref = type_info["items"]["$ref"]
                _type = f'List[{self.get_forward_ref(ref, domain_name)}]'
            else:
                _type = f'List[{self.convert_js_to_py_type(type_info["items"]["type"], domain_name)}]'
        else:
            if '$ref' in type_info:
                _type = self.get_forward_ref(type_info["$ref"], domain_name)
            else:
                if 'enum' in type_info:
                    _enum_vals = ", ".join([f'\'{x}\'' for x in type_info["enum"]])
                    _type = f'Literal[{_enum_vals}]'
                else:
                    _type = self.js_to_py_types[type_info['type']]

        return _type

    @staticmethod
    def get_forward_ref(relative_ref: str, potential_domain_context: str):
        """
        Generates a forward absolute forward reference to Protocol class attr. If the reference is relative
        to a nested class, the full path is resolved against potential_domain_context. In the case of
        the reference being relative to the Protocol class, the path is simple resolved against the Protocol class

        :param relative_ref: reference to another class, in the form of foo or foo.bar
        :param potential_domain_context: context to resolve class against if relative_ref is relative to it
        :return: absolute forward reference to nested class attr
        """
        if len(relative_ref.split('.')) == 2:
            non_fw_ref = f'Protocol.{relative_ref}'
        else:
            non_fw_ref = f'Protocol.{potential_domain_context}.{relative_ref}'
        return f'\'{non_fw_ref}\''


class TypingCodeGenerator:
    def __init__(self):
        self.indent_manager = IndentManager()
        self.temp_lines_classification = partial(temp_var_change, self, 'lines_classification')

        self.import_lines = []
        self.inserted_lines = []
        self.code_lines = []
        self.lines_classification = 'code'
        self.init_imports()

    def init_imports(self):
        with self.temp_lines_classification('import'):
            self.add('import sys')
            self.add_newlines(num=1)
            self.add('from typing import List, Dict, Any')
            self.add_newlines(num=1)
            self.add('if sys.version_info < (3,8):')
            with self.indent_manager:
                self.add('from typing_extensions import Literal, TypedDict')
            self.add('else:')
            with self.indent_manager:
                self.add('from typing import Literal, TypedDict')
            self.add_newlines(num=2)

    def add_newlines(self, num: int = 1):
        self.add('\n' * num)

    def add_newlines_before_code(self, num: int = 1):
        with self.temp_lines_classification('inserted'):
            self.add('\n' * num)

    def add_comment_from_info(self, info: Dict[str, Any]):
        if 'description' in info:
            newline = '\n'
            self.add(f'# {info["description"].replace(newline, " ")}')

    def add_doc_string_from_info(self, info: Dict[str, Any]):
        if 'description' in info:
            self.add(f'"""{info["description"]}"""')

    def add(self, code: str = None, lines: List[str] = None):
        if code:
            preprocessed = [line for line in dedent(code).split('\n')]
            lines = [f'{self.indent_manager}{li}' for li in preprocessed]
        self.__getattribute__(f'{self.lines_classification}_lines').extend(lines)

    def insert_before_code(self, other: Union['TypingCodeGenerator', 'str']):
        with self.temp_lines_classification('inserted'):
            if isinstance(other, str):
                self.add(other)
            else:
                self.add(lines=other.code_lines)

    def __str__(self):
        return '\n'.join(self.import_lines) + '\n'.join(self.inserted_lines) + '\n'.join(self.code_lines)


class TypedDictGenerator(TypingCodeGenerator):
    def __init__(self, name: str, total: bool):
        super().__init__()
        self.name = name
        self.total = total
        total_spec = ', total=False' if total else ''
        self.add(f'class {name}(TypedDict{total_spec}):')

    def copy_with_filter(self, new_name, sub_p, sub_r):
        inst = TypedDictGenerator(new_name, self.total)
        for line in self.code_lines[1:]:
            inst.code_lines.append(re.sub(sub_p, sub_r, line))
        return inst

    def init_imports(self):
        pass

    def __repr__(self):
        return f'<TypedDictGenerator {self.name}>'


class IndentManager:
    def __init__(self):
        self._indent = ''

    def __enter__(self):
        self._indent += '    '

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._indent = self._indent[:-4]

    def __str__(self):
        return self._indent


@contextmanager
def temp_var_change(cls_instance: object, var: str, value: Any):
    initial = cls_instance.__getattribute__(var)
    yield cls_instance.__setattr__(var, value)
    cls_instance.__setattr__(var, initial)


if __name__ == '__main__':
    generator = ProtocolTypesGenerator()
    generator.retrieve_top_level_domain()
    generator.gen_spec()
    generator.write_generated_code()
