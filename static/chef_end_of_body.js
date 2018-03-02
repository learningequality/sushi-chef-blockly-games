// This gets sourced as the last tag in <body>, so it will get executed after the
// document finishes loading but before any $(document).ready() callbacks.
setTimeout(function() {
  removeElement('languageMenu');
  removeElement('title');
  removeElement('linkButton');
}, 200);

function removeElement(id) {
  var elem = document.getElementById(id);
  if (elem) {
    elem.remove();
  }
}
