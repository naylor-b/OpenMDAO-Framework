
var openmdao = (typeof openmdao == "undefined" || !openmdao ) ? {} : openmdao ; 

openmdao.ComponentEditor = function(model,pathname) {
    // TODO: hack alert... mangling pathname
    openmdao.ComponentEditor.prototype.init.call(this,'-'+pathname.replace(/\./g,'-'),'Component');
    
    /***********************************************************************
     *  private
     ***********************************************************************/
     
    // initialize private variables
    var self = this
        
    model.addListener(update)
          
    /** load the table with the given properties */
    function loadTabs(properties) {
        var style = 'style="padding:5px;background-color:#6a6a6a;border:1px solid #fff;"',
            dl = jQuery('<dl id="'+self.id+'_tabs"></dl>');
            
        self.elm.html("");
        self.elm.append(dl);
        
        var tabcount = 0;
        
        jQuery.each(properties,function (name,val) {
            if (name !== 'type') {
                tabcount = tabcount + 1;
                
                if (name.length > 10) {
                    tabname = name.substr(0,10);
                }
                else {
                    tabname = name;
                }
                
                var dt = jQuery('<dt id="'+self.id+'_'+name+'_tab" target="'+self.id+'_'+name+'_pane">'+tabname+'</dt>'),
                    dd = jQuery('<dd id="'+self.id+'_'+name+'_pane"></dd>'),
                    contentPane = jQuery('<div id="'+self.id+'_'+name+'" '+style+'></div>');
                                    
                dl.append(dt);
                dl.append(dd);
                dd.append(contentPane)
                
                getContent(contentPane,name,val)
            }
        });
        
        self.elm.width((tabcount+1)*75);

        openmdao.TabbedPane(self.id);
    }
    
    /** populate content pane appropriately for the content */
    function getContent(contentPane,name,val) {
        if (name == 'Outputs') {
            new openmdao.PropertiesPane(contentPane,model,pathname,name,val,false);
        }
        else {
            new openmdao.PropertiesPane(contentPane,model,pathname,name,val,true);
        }
    }
    
    /** if there is an object loaded, update it from the model */
    function update() {
        if (self.pathname && self.pathname.length>0)
            self.editObject(self.pathname)
    }
    
    /***********************************************************************
     *  privileged
     ***********************************************************************/
    
    /** get the specified object from model, load properties into tabs */
    this.editObject = function(path) {
        if (self.pathname !== path)
            self.pathname = path
        model.getComponent(path, loadTabs,
            function(jqXHR, textStatus, errorThrown) {
                self.pathname = ''
                alert("Error getting properties for "+self.pathname+" (status="+jqXHR.status+"): "+jqXHR.statusText)
                openmdao.Util.htmlWindow(jqXHR.responseText,'Error getting properties',600,400)
                debug.error(jqXHR,textStatus,errorThrown)
            }
        )
        return this
    }

    if (pathname) {
        this.editObject(pathname)
    }

}

/** set prototype */
openmdao.ComponentEditor.prototype = new openmdao.BasePane();
openmdao.ComponentEditor.prototype.constructor = openmdao.ComponentEditor;