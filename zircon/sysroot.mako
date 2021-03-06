<%include file="header.mako" />

_out_dir = get_label_info(":bogus", "target_out_dir")

<%def name="copy_target(path)">${"copy_%s" % path.replace('/', '_').replace('.', '_')}</%def>

% for path, file in sorted(data.files.iteritems()):
copy("${copy_target(path)}") {
  sources = [
    "${file}",
  ]

  outputs = [
    "$_out_dir/sysroot/${path}",
  ]
}

% endfor

group("sysroot") {

  deps = [
    % for path, file in sorted(data.files.iteritems()):
    ":${copy_target(path)}",
    % endfor
  ]
}

