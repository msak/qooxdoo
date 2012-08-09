#!/usr/bin/env python
# -*- coding: utf-8 -*-
################################################################################
#
#  qooxdoo - the new era of web development
#
#  http://qooxdoo.org
#
#  Copyright:
#    2006-2012 1&1 Internet AG, Germany, http://www.1und1.de
#
#  License:
#    LGPL: http://www.gnu.org/licenses/lgpl.html
#    EPL: http://www.eclipse.org/org/documents/epl-v10.php
#    See the LICENSE file in the project's top-level directory for details.
#
#  Authors:
#    * Thomas Herchenroeder (thron7)
#
################################################################################

##
# AST checking, for unknown globals etc.
##

import os, sys, re, types
from collections import defaultdict
from ecmascript.frontend import treeutil, lang, Comment
from ecmascript.frontend import tree, treegenerator
from ecmascript.transform.optimizer import variantoptimizer
from ecmascript.transform.evaluate  import evaluate
from ecmascript.transform.check  import scopes
from generator.Context import console

class LintChecker(treeutil.NodeVisitor):

    def __init__(self, root_node, file_name, opts):
        super(LintChecker, self).__init__()
        self.root_node = root_node
        self.file_name = file_name  # it's a warning module, so i need a proper file name
        self.opts = opts

    def visit_file(self, node):
        # we can run the basic scope checks as with function nodes
        self.function_unknown_globals(node)
        self.function_unused_vars(node)
        self.function_used_deprecated(node)
        self.function_multiple_var_decls(node)
        # this is also good to check class map integrity
        for class_defn in treeutil.findQxDefineR(node):
            self.class_declared_privates(class_defn)
            self.class_reference_fields(class_defn)
        # check all qx.core.Environment calls
        self.environment_check_calls(node)
        # recurse
        for cld in node.children:
            self.visit(cld)

    def visit_map(self, node):
        #print "visiting", node.type
        self.map_unique_keys(node)
        # recurse
        for cld in node.children:
            self.visit(cld)

    def visit_loop(self, node):
        #print "visiting", node.type
        self.loop_body_block(node.getChild("body")) # all "loops" have at least one body
        if node.get("loopType")=="IF" and len(node.children)>2:  # there is an "else"
            self.loop_body_block(node.children[2])
        # recurse
        for cld in node.children:
            self.visit(cld)
        
    def visit_function(self, node):
        #print "visiting", node.type
        self.function_unknown_globals(node)
        self.function_unused_vars(node)
        self.function_used_deprecated(node)
        self.function_multiple_var_decls(node)
        # recurse
        for cld in node.children:
            self.visit(cld)
        
    def visit_TEMPLATE(self, node):
        #print "visiting", node.type
        # recurse
        for cld in node.children:
            self.visit(cld)
        

    # - ---------------------------------------------------------------------------

    def function_used_deprecated(self, funcnode):
        # take advantage of Scope() objects
        scope = funcnode.scope
        for id_, scopeVar in scope.globals().items():
            # id_ might be an incomplete class id, like "qx" 
            # let's look at the var uses
            for var_node in scopeVar.uses:
                full_name = (treeutil.assembleVariable(var_node))[0]
                ok = True
                if (full_name in lang.GLOBALS # JS built-ins ('alert' etc.)
                        and full_name in lang.DEPRECATED):
                    ok = False
                    at_hints = get_at_hints(funcnode) # check full_name against @ignore hints
                    if at_hints:
                        ok = self.is_name_lint_filtered(full_name, at_hints, "ignoreDeprecated")
                if not ok:
                    warn("Deprecated global symbol used: %s" % full_name, self.file_name, var_node)
                    
    def function_unknown_globals(self, funcnode):
        # take advantage of Scope() objects
        scope = funcnode.scope
        for id_, scopeVar in scope.globals().items():
            if id_ in self.opts.allowed_globals:
                continue
            elif id_ in lang.GLOBALS: # JS built-ins ('alert' etc.)
                continue
            else:  
                # we want to be more specific than just the left-most symbol,
                # like "qx", so let's look at the var uses
                for var_node in scopeVar.uses:
                    var_top = treeutil.findVarRoot(var_node)
                    full_name = (treeutil.assembleVariable(var_top))[0]
                    ok = False
                    if extension_match_in(full_name, self.opts.library_classes + 
                        self.opts.class_namespaces): # known classes (classList + their namespaces)
                        ok = True
                    else:
                        at_hints = get_at_hints(funcnode) # check full_name against @ignore hints
                        if at_hints:
                            ok = self.is_name_lint_filtered(full_name, at_hints, "ignoreUndefined")
                    if not ok:
                        #if self.file_name == "feedreader.simulation.ria.FeedreaderAbstract" and full_name == "treeLocator":
                        #    import pydb; pydb.debugger()
                        warn("Unknown global symbol used: %s" % full_name, self.file_name, var_node)
                    
    def function_unused_vars(self, funcnode):
        scope = funcnode.scope
        unused_vars = dict([(id_, scopeVar) for id_, scopeVar in scope.vars.items() 
                                if self.var_unused(scopeVar)])

        for var_name,scopeVar in unused_vars.items():
            ok = False
            at_hints = get_at_hints(funcnode) # check @ignore hints
            if at_hints:
                ok = self.is_name_lint_filtered(var_name, at_hints, "ignoreUnused")
            if not ok:
                warn("Declared but unused variable or parameter '%s'" % var_name, self.file_name, scopeVar.decl[0])

    ##
    # Checks the @lint hints in <at_hints> if the given <var_name> is filtered
    # under the <filter_key> (e.g. "ignoreUndefined" in *@lint ignoreUndefined(<var_name>))
    #
    def is_name_lint_filtered(self, var_name, at_hints, filter_key):
        def matches(name, prefix):
            return re.match(r"%s\b" % prefix, name)
        filtered = False
        if at_hints:
            if ( 'lint' in at_hints and 
                filter_key in at_hints['lint']):
                if any([matches(var_name, x) for x in at_hints['lint'][filter_key]]):
                    filtered = True
        return filtered


    ##
    # Check if a map only has unique keys.
    #
    def map_unique_keys(self, node):
        # all children are .type "keyvalue", with .get(key) = <identifier>
        entries = [(keyval.get("key"), keyval) for keyval in node.children]
        seen = set()
        for key,keyval in entries:
            if key in seen:
                warn("Duplicate use of map key", self.file_name, keyval)
            seen.add(key)

    def function_multiple_var_decls(self, node):
        scope_node = node.scope
        for id_, var_node in scope_node.vars.items():
            if self.multiple_var_decls(var_node):
                warn("Multiple declarations of variable '%s' (%r)" % (
                    id_, [(n.get("line",0) or -1) for n in var_node.decl]), self.file_name, None)

    def multiple_var_decls(self, scopeVar):
        return len(scopeVar.decl) > 1

    def var_unused(self, scopeVar):
        return len(scopeVar.decl) > 0 and len(scopeVar.uses) == 0

    def loop_body_block(self, body_node):
        if not body_node.getChild("block",0):
            ok = False
            scope_node = scopes.find_enclosing(body_node)
            if scope_node:
                at_hints = get_at_hints(scope_node.node)
                if at_hints and 'lint' in at_hints and 'ignoreNoLoopBlock' in at_hints['lint']:
                    ok = True
            if not ok:
                warn("Loop or condition statement without a block as body", self.file_name, body_node)

    ##
    # Check that no privates are used in code that are not declared as a class member
    #
    this_aliases = ('this', 'that')
    reg_privs = re.compile(r'\b__')

    def class_declared_privates(self, class_def_node):
        try:
            class_map = treeutil.getClassMap(class_def_node)
        except tree.NodeAccessException:
            return

        # statics
        private_keys = set()
        # collect all privates
        if 'statics' in class_map:
            for key in class_map['statics']:
                if self.reg_privs.match(key):
                    private_keys.add(key)
            # go through uses of 'this' and 'that' that reference a private
            for key,val in class_map['statics'].items():
                if val.type == 'function':
                    function_privs = self.function_uses_local_privs(val)
                    for priv, node in function_privs:
                        if priv not in private_keys:
                            warn("Using an undeclared private class feature: '%s'" % priv, self.file_name, node)
        
        # members
        private_keys = set()
        # collect all privates
        if 'members' in class_map:
            for key in class_map['members']:
                if self.reg_privs.match(key):
                    private_keys.add(key)
            # go through uses of 'this' and 'that' that reference a private
            for key,val in class_map['members'].items():
                if val.type == 'function':
                    function_privs = self.function_uses_local_privs(val)
                    for priv, node in function_privs:
                        if priv not in private_keys:
                            warn("Using an undeclared private class feature: '%s'" % priv, self.file_name, node)


    ##
    # Warn about reference types in map values, as they are shared across instances.
    #
    def class_reference_fields(self, class_def_node):
        try:
            class_map = treeutil.getClassMap(class_def_node)
        except tree.NodeAccessException:
            return
        # only check members
        members_map = class_map['members'] if 'members' in class_map else {}

        for key, value in members_map.items():
            if (value.type in ("map", "array") or
               (value.type == "operation" and value.get("operator")=="NEW")):
               warn("Reference values are shared across all instances: '%s'" % key, self.file_name, value)


    def function_uses_local_privs(self, func_node):
        function_privs = set()
        reg_this_aliases = re.compile(r'\b%s' % "|".join(self.this_aliases))
        scope = func_node.scope
        for id_,scopeVar in scope.vars.items():
            if reg_this_aliases.match(id_):
                for var_use in scopeVar.uses:
                    full_name = treeutil.assembleVariable(var_use)[0]
                    name_parts = full_name.split(".")
                    if len(name_parts) > 1 and self.reg_privs.match(name_parts[1]):
                        function_privs.add((name_parts[1],var_use))
        return function_privs

    def environment_check_calls(self, node):
        for env_call in variantoptimizer.findVariantNodes(node):
            variantMethod = env_call.toJS(treegenerator.PackerFlags).rsplit('.',1)[1]
            callNode = treeutil.selectNode(env_call, "../..")
            if variantMethod in ["select"]:
                self.environment_check_select(callNode)
            elif variantMethod in ["get"]:
                self.environment_check_get(callNode)
            elif variantMethod in ["filter"]:
                self.environment_check_filter(callNode)

    def environment_check_select(self, select_call):
        if select_call.type != "call":
            return False
            
        params = select_call.getChild("arguments")
        if len(params.children) != 2:
            warn("qx.core.Environment.select: takes exactly two arguments.", self.file_name, select_call)
            return False

        # Get the variant key from the select() call
        firstParam = params.getChildByPosition(0)
        #evaluate.evaluate(firstParam)
        #firstValue = firstParam.evaluated
        #if firstValue == () or not isinstance(firstValue, types.StringTypes):
        if not treeutil.isStringLiteral(firstParam):
            warn("qx.core.Environment.select: first argument is not a string literal.", self.file_name, select_call)
            return False

        # Get the resolution map, keyed by possible variant key values (or value expressions)
        secondParam = params.getChildByPosition(1)
        default = None
        found = False
        if secondParam.type == "map":
            # we could try to check a relevant key from a variantsMap against the possibilities in the code
            # like in variantoptimzier - deferred
            pass
        else:
            warn("qx.core.Environment.select: second parameter not a map.", self.file_name, select_call)


    def environment_check_get(self, get_call):

        # Simple sanity checks
        params = get_call.getChild("arguments")
        if len(params.children) != 1:
            warn("qx.core.Environment.get: takes exactly one arguments.", self.file_name, get_call)
            return False

        firstParam = params.getChildByPosition(0)
        if not treeutil.isStringLiteral(firstParam):
            warn("qx.core.Environment.get: first argument is not a string literal.", self.file_name, get_call)
            return False

        # we could try to verify the key, like in variantoptimizer


    def environment_check_filter(self, filter_call):

        def isExcluded(mapkey, variantMap):
            return mapkey in variantMap and bool(variantMap[mapkey]) == False

        complete = False
        if filter_call.type != "call":
            return complete

        params = filter_call.getChild("arguments")
        if len(params.children) != 1:
            warn("qx.core.Environment.filter: takes exactly one arguments.", self.file_name, filter_call)
            return complete

        # Get the map from the filter call
        firstParam = params.getChildByPosition(0)
        if not firstParam.type == "map":
            warn("qx.core.Environment.filter: first argument is not a map.", self.file_name, filter_call)
            return complete

        # we could now try to verify the keys in the map - deferred

        return True

# - ---------------------------------------------------------------------------

def warn(msg, fname, node):
    if node:
        emsg = "%s (%s,%s): %s" % (fname, node.get("line"), node.get("column"), msg)
    else:
        emsg = "%s: %s" % (fname, msg)
    if console:
        console.warn(emsg)
    else:
        print >>sys.stderr, emsg

##
# Get the JSDoc comments in a nested dict structure
def get_at_hints(node, at_hints=None):
    if at_hints is None:
        at_hints = defaultdict(dict)
    commentAttributes = Comment.parseNode(node)  # searches comment "around" this node
    for entry in commentAttributes:
        cat = entry['category']
        if cat=='lint':
             # {'arguments': ['a', 'b'],
             #  'category': u'lint',
             #  'functor': u'ignoreReferenceField',
             #  'text': u'<p>ignoreReferenceField(a,b)</p>'
             # }
            functor = entry['functor']
            if functor not in at_hints['lint']:
                at_hints['lint'][functor] = set()
            at_hints['lint'][functor].update(entry['arguments']) 
    # include @hints of parent scopes
    scope = scopes.find_enclosing(node)
    #import pydb; pydb.debugger()
    if scope:
        at_hints = get_at_hints(scope.node, at_hints)
    return at_hints


def defaultOptions():
    class C(object): pass
    opts = C()
    opts.library_classes = []
    opts.class_namespaces = []
    opts.allowed_globals = []
    return opts

##
# Check if a name is in a list, or is a (dot-exact) extension of any of the
# names in the list (i.e. extension_match_in("foo.bar.baz", ["foo.bar"]) ==
# True).
#
# (This is a copy of MClassDependencies._splitQxClass).
#
def extension_match_in(name, name_list):
    res_name = ''
    res_attribute = ''
    if name in name_list:
        res_name = name
    # see if name is a (dot-exact) prefix of any of name_list
    elif "." in name:
        for list_name in name_list:
            if name.startswith(list_name) and re.match(r'%s\b' % list_name, name):
                if len(list_name) > len(res_name): # take the longest match
                    res_name = list_name
                    ## compute the 'attribute' suffix
                    #res_attribute = name[ len(list_name) +1:]  # skip list_name + '.'
                    ## see if res_attribute is chained, too
                    #dotidx = res_attribute.find(".")
                    #if dotidx > -1:
                    #    res_attribute = res_attribute[:dotidx]    # only use the first component

    return res_name

# - ---------------------------------------------------------------------------

def lint_check(node, file_name, opts):
    lint = LintChecker(node, file_name, opts)
    lint.visit(node)